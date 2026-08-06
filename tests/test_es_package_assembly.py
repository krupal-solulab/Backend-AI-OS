"""E&S Package Assembly eval test — proves the pipeline against the REAL
Workflow_11 dataset (originally ``Data sets/Workflow 2/package_assembly_dataset``,
copied to ``TEST_DATA_ROOT/Workflow_11/test_dataset`` per DATA_AND_FIXTURES.md;
see that folder's Validation_Rules_Test_Dataset.md for the expected-outcome
spec). Also depends on the real Workflow_10 dataset (Market Matching) since
this workflow re-derives field-level extracted data from it — see
submission_resolver.py's docstring.

Scenario 04 is a MANDATORY, non-skippable release-gate case per the PRD's
Section 9 risk register (the auto-fill grounding boundary is the single
highest-consequence rule in this workflow) — it gets extra scrutiny below,
not just a status assertion.

Pytest-discovered here (not under src/verticals/es/...) — see
verticals/es/workflows/package_assembly/eval_test.py for why.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, RawDocument, WorkflowInput
from core.common.enums import DecisionOutcome, DocumentKind, ReviewAction, ReviewStatus, Role, Vertical
from core.config import get_settings
from core.documents import LocalDocumentStore
from core.extraction import DefaultExtractionService
from core.ingestion import MockConnectorService
from core.llm import LLMService, MockLLMProvider, build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.models import Tenant
from core.review_queue import DefaultReviewQueueService
from core.rules_engine import DefaultRulesEngine
from verticals.es.workflows.market_matching.service import (
    DEFAULT_WORKFLOW_N as MM_WORKFLOW_N,
)
from verticals.es.workflows.market_matching.service import MarketMatchingPipeline
from verticals.es.workflows.package_assembly.live_ingestion import (
    build_live_carrier_view,
    build_live_extracted_model,
)
from verticals.es.workflows.package_assembly.router import (
    RunFromMarketMatchingRequest,
    run_package_assembly_from_market_matching,
)
from verticals.es.workflows.package_assembly.service import PackageAssemblyPipeline

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_10/11 fixtures unavailable",
)


@pytest.fixture
def es_ctx() -> Ctx:
    return Ctx(tenant_id="demo-es", vertical=Vertical.ES, user_id="u-jr", role=Role.JUNIOR)


@pytest.fixture
async def es_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(Tenant(id="demo-es", name="Demo E&S", vertical=Vertical.ES))
        await session.commit()
        yield session
    await engine.dispose()


def _pipeline() -> PackageAssemblyPipeline:
    return PackageAssemblyPipeline(extraction=DefaultExtractionService(), llm=build_llm_service())


async def _run(ctx: Ctx, scenario_ref: str, carrier_id: str | None = None):
    pipeline = _pipeline()
    params = {"carrier_id": carrier_id} if carrier_id else {}
    return await pipeline.run(ctx, WorkflowInput(source_ref=scenario_ref, params=params))


async def test_scenario_01_disclosed_gap_not_blocking(es_ctx) -> None:
    """Vantage: a third-party-only actuarial summary is missing — disclosed
    (READY_WITH_GAP), never treated as a blocker."""
    output = await _run(es_ctx, "scenario_01")
    assert output.decision.outcome is DecisionOutcome.PROCEED
    assert output.payload["status"] == "READY_WITH_GAP"
    assert output.payload["blocking_items"] == []
    gap_items = [g["item"] for g in output.payload["gap_items_disclosed"]]
    assert any("actuarial" in g.lower() for g in gap_items)


async def test_scenario_02_blocked_two_reasons(es_ctx) -> None:
    """Palmetto: loss-run shortfall + an unanswerable narrative questionnaire
    — both block; the package must never be presented as ready."""
    output = await _run(es_ctx, "scenario_02")
    assert output.decision.outcome is DecisionOutcome.REQUEST_INFO
    assert output.payload["status"] == "BLOCKED"
    blocking = [b["item"] for b in output.payload["blocking_items"]]
    assert len(blocking) == 2
    assert any("loss run" in b.lower() for b in blocking)
    assert any("questionnaire" in b.lower() for b in blocking)


async def test_scenario_03_same_submission_different_carrier_outcomes(es_ctx) -> None:
    """The core per-carrier-independence test: the SAME loss-run data is a
    non-issue for Ironclad (3yr requirement, met) and a blocker for Meridian
    (5yr requirement, short) — a naive shared-template implementation would
    get this wrong."""
    ironclad = await _run(es_ctx, "scenario_03", carrier_id="CAR-03")
    meridian = await _run(es_ctx, "scenario_03", carrier_id="CAR-01")

    assert ironclad.payload["status"] == "READY"
    assert ironclad.payload["blocking_items"] == []

    assert meridian.payload["status"] == "BLOCKED"
    assert any("loss run" in b["item"].lower() for b in meridian.payload["blocking_items"])

    # The differentiation itself: the loss-run concern must appear in
    # Meridian's facts/blocking items and NOT in Ironclad's.
    ironclad_text = str(ironclad.payload["blocking_items"]) + str(
        ironclad.payload["gap_items_disclosed"]
    )
    assert "loss run" not in ironclad_text.lower()


async def test_scenario_04_auto_fill_boundary_release_gate(es_ctx) -> None:
    """MANDATORY release-gate case (PRD Section 9): resist the plausible
    shortcut of estimating unit_count from TIV/class even though the
    carrier's own metadata calls it auto-fillable. Verified against the
    REAL Oakwood SOV text, which genuinely has no unit-count line."""
    output = await _run(es_ctx, "scenario_04")
    assert output.payload["status"] == "READY"
    assert output.payload["blocking_items"] == []

    fields_by_name = {f["field_name"]: f for f in output.payload["supplemental_form_fields"]}
    assert set(fields_by_name) == {
        "year_built", "construction_type", "sprinklered",
        "total_insurable_value", "unit_count_estimate_from_TIV_and_class",
    }

    # The four genuinely extractable fields ARE auto-filled, each with a real citation.
    # (total_insurable_value comes back as extraction's own numeric coercion,
    # 18000000.0, not a reformatted "$18,000,000" string — this test asserts
    # the real pass-through value, not an imagined display format.)
    for name, expected_value in [
        ("year_built", "2010"),
        ("sprinklered", "Yes"),
        ("total_insurable_value", "18000000.0"),
    ]:
        f = fields_by_name[name]
        assert f["auto_filled"] is True, f"{name} should be auto-filled from the real SOV"
        assert f["value"] == expected_value
        assert f["source_citation"], f"{name} must carry a real citation"

    assert "masonry" in fields_by_name["construction_type"]["value"].lower()
    assert fields_by_name["construction_type"]["auto_filled"] is True

    # THE hard requirement: unit_count must NEVER be auto-filled, no matter
    # what the carrier's metadata claims, because there is no direct source.
    unit_count = fields_by_name["unit_count_estimate_from_TIV_and_class"]
    assert unit_count["auto_filled"] is False
    assert unit_count["value"] is None
    assert unit_count["source_citation"] is None


async def test_scenario_05_proactive_disclosure_and_diligent_search_passthrough(es_ctx) -> None:
    """Summit Roofing -> Ironclad: diligent-search is a verbatim pass-through
    from the input (documentation_status='present'), never recomputed."""
    output = await _run(es_ctx, "scenario_05")
    assert output.payload["status"] == "READY"
    assert output.payload["diligent_search_attached"] is True
    assert output.payload["blocking_items"] == []


async def test_scenario_06_clean_baseline(es_ctx) -> None:
    output = await _run(es_ctx, "scenario_06")
    assert output.decision.outcome is DecisionOutcome.PROCEED
    assert output.payload["status"] == "READY"
    assert output.payload["blocking_items"] == []
    assert output.payload["gap_items_disclosed"] == []


async def test_full_pipeline_review_queue_and_audit(es_ctx, es_session) -> None:
    """package() -> review_queue.enqueue() -> act() -> audit, end to end,
    for one real scenario."""
    output = await _run(es_ctx, "scenario_06")
    assert output.draft is not None and output.draft.text

    rq = DefaultReviewQueueService()
    item = await rq.enqueue(es_session, es_ctx, output, "package_assembly")
    assert item.status is ReviewStatus.PENDING

    acted = await rq.act(es_session, es_ctx, item.id, ReviewAction.APPROVE)
    assert acted.status is ReviewStatus.APPROVED

    audit = DefaultAuditService()
    await audit.record(
        es_session, es_ctx,
        AuditEntry(
            actor="ai", who="system", what=f"decision={output.decision.outcome.value}",
            workflow="package_assembly", tenant_id=es_ctx.tenant_id, vertical=es_ctx.vertical,
        ),
    )
    entries = await audit.query(es_session, es_ctx, {"workflow": "package_assembly"})
    assert len(entries) == 1


async def test_run_from_market_matching_uses_real_carrier_and_document_data(
    es_ctx, es_session
) -> None:
    """Market Matching -> Package Assembly (Phase 2 connectivity): a real
    package for CAR-03 (Ironclad) built from an ACTUAL Market Matching item
    for submission_01, not the Workflow_11 fixture. Confirms real
    carrier_id/carrier_name/required_documents (from the real Carrier
    Appetite Profile panel) and real document completeness (submission_01's
    actually-persisted acord/loss_run/financials satisfy all of Ironclad's
    real requirements)."""
    mm_pipeline = MarketMatchingPipeline(
        session=es_session,
        connector=MockConnectorService(workflow_n=MM_WORKFLOW_N),
        extraction=DefaultExtractionService(),
        rules_engine=DefaultRulesEngine(),
        llm=build_llm_service(),
        documents=LocalDocumentStore(),
        workflow_n=MM_WORKFLOW_N,
    )
    mm_output = await mm_pipeline.run(
        es_ctx, WorkflowInput(submission_id="submission_01", source_ref="submission_01")
    )
    mm_item = await DefaultReviewQueueService().enqueue(
        es_session, es_ctx, mm_output, "market_matching"
    )

    items = await run_package_assembly_from_market_matching(
        RunFromMarketMatchingRequest(market_matching_review_item_id=mm_item.id, carrier_id="CAR-03"),
        es_ctx, es_session,
    )
    assert len(items) == 1
    payload = items[0].payload
    assert payload.carrier_id == "CAR-03"
    assert payload.carrier_name == "Ironclad Casualty Solutions"

    checklist = {d.document_type: d.included for d in payload.document_checklist}
    assert checklist == {
        "ACORD 125": True, "ACORD 126": True,
        "3-year loss run minimum": True, "current financials": True,
    }
    # No real Diligent Search record exists yet, and Market Matching's own
    # signal says required=True/not yet compliant for this submission — a
    # real, correct blocking condition, not a bug in the live path.
    assert any("Diligent search" in b.item for b in payload.blocking_items)


async def test_run_from_market_matching_reflects_real_diligent_search_record(
    es_ctx, es_session
) -> None:
    """PA-06 (Phase 2 connectivity): once a real Diligent Search record with
    a SUFFICIENT state exists for the submission, the live Package Assembly
    path reflects it as present — no more diligent-search blocking item."""
    mm_pipeline = MarketMatchingPipeline(
        session=es_session,
        connector=MockConnectorService(workflow_n=MM_WORKFLOW_N),
        extraction=DefaultExtractionService(),
        rules_engine=DefaultRulesEngine(),
        llm=build_llm_service(),
        documents=LocalDocumentStore(),
        workflow_n=MM_WORKFLOW_N,
    )
    mm_output = await mm_pipeline.run(
        es_ctx, WorkflowInput(submission_id="submission_01", source_ref="submission_01")
    )
    mm_item = await DefaultReviewQueueService().enqueue(
        es_session, es_ctx, mm_output, "market_matching"
    )

    ds_pkg = OutputPackageRow(
        tenant_id=es_ctx.tenant_id,
        submission_id="submission_01",
        workflow="diligent_search",
        payload={
            "compliance_record_id": "ds-1",
            "submission_id": "submission_01",
            "state_determinations": [
                {
                    "state": "FL", "requirement_status": "REQUIRED",
                    "sufficiency_status": "SUFFICIENT",
                }
            ],
            "overall_status": "COMPLETE",
        },
    )
    es_session.add(ds_pkg)
    await es_session.flush()
    es_session.add(ReviewItemRow(
        tenant_id=es_ctx.tenant_id, submission_id="submission_01",
        output_package_id=ds_pkg.id, workflow="diligent_search", status=ReviewStatus.PENDING,
    ))
    await es_session.commit()

    items = await run_package_assembly_from_market_matching(
        RunFromMarketMatchingRequest(market_matching_review_item_id=mm_item.id, carrier_id="CAR-03"),
        es_ctx, es_session,
    )
    payload = items[0].payload
    assert payload.diligent_search_attached is True


async def test_run_live_cover_letter_is_grounded_in_real_data(es_ctx, es_session) -> None:
    """Part 4 fix: the live path's cover letter must be grounded in this
    submission's REAL named insured, REAL carrier appetite notes, and REAL
    loss-run figures — not the generic "[Your Name]"/"[Carrier's Name]"
    placeholder template it produced before this fix (confirmed live against
    a real Gmail submission). Uses MockLLMProvider (deterministic fact-echo,
    see core/llm/service.py) instead of a real API call, so this asserts on
    exactly what reached the draft step, not on how a real LLM phrases it.

    submission_01 (Delta Electric Services LLC) -> CAR-03 (Ironclad) is a
    real match from the Workflow_10 dataset, and Ironclad is the one real
    carrier profile in this panel with a non-empty ``notes`` field."""
    mm_pipeline = MarketMatchingPipeline(
        session=es_session,
        connector=MockConnectorService(workflow_n=MM_WORKFLOW_N),
        extraction=DefaultExtractionService(),
        rules_engine=DefaultRulesEngine(),
        llm=build_llm_service(),
        documents=LocalDocumentStore(),
        workflow_n=MM_WORKFLOW_N,
    )
    mm_output = await mm_pipeline.run(
        es_ctx, WorkflowInput(submission_id="submission_01", source_ref="submission_01")
    )
    mm_item = await DefaultReviewQueueService().enqueue(
        es_session, es_ctx, mm_output, "market_matching"
    )

    pa_pipeline = PackageAssemblyPipeline(
        extraction=DefaultExtractionService(),
        llm=LLMService(MockLLMProvider()),
    )
    output = await pa_pipeline.run_live(es_ctx, es_session, mm_item.id, "CAR-03")

    letter = output.draft.text
    assert "Delta Electric Services LLC" in letter
    assert "steep-slope roofing" in letter or "higher severity" in letter
    assert "18400" in letter


async def test_document_availability_uses_content_classification_not_filename(
    es_ctx, es_session
) -> None:
    """Regression: caught live against a real Gmail submission — a real
    attachment named e.g. "harborview_loss_run.pdf" doesn't match the
    fixture loader's exact stem ("loss_run.pdf"), so it's stored as
    DocumentKind.OTHER at ingestion. Document availability must be derived
    from the extraction service's real CONTENT-based reclassification
    (documents.<kind>.present, computed by build_live_extracted_model), not
    from each Document row's raw, filename-derived kind column — otherwise
    every requirement reads as missing despite the real document being on
    file, exactly what happened before this fix."""
    mm_pkg = OutputPackageRow(
        tenant_id=es_ctx.tenant_id,
        submission_id="SUB-TEST-PREFIXED-FILENAME",
        workflow="market_matching",
        payload={
            "submission_id": "SUB-TEST-PREFIXED-FILENAME",
            "matches": [
                {"carrier_id": "CAR-03", "carrier_name": "Ironclad Casualty Solutions",
                 "score": 1.0, "missing": [], "flags": []}
            ],
            "excluded": [],
            "diligent_search": {"required": False, "on_file": 0, "compliant": True, "note": ""},
        },
    )
    es_session.add(mm_pkg)
    await es_session.flush()
    mm_item = ReviewItemRow(
        tenant_id=es_ctx.tenant_id, submission_id="SUB-TEST-PREFIXED-FILENAME",
        output_package_id=mm_pkg.id, workflow="market_matching", status=ReviewStatus.PENDING,
    )
    es_session.add(mm_item)
    await es_session.commit()

    await LocalDocumentStore().save(
        es_session, es_ctx, "SUB-TEST-PREFIXED-FILENAME",
        RawDocument(
            kind=DocumentKind.OTHER,  # exactly what live ingestion stores for this filename
            filename="harborview_loss_run.pdf",
            content="Loss Run - 5 Year History\nTotal Incurred (3yr): $18,400\n",
        ),
    )

    model = await build_live_extracted_model(es_session, es_ctx, "SUB-TEST-PREFIXED-FILENAME")
    view = await build_live_carrier_view(
        es_session, es_ctx, mm_item.id, "CAR-03", extracted_model=model
    )
    assert "loss_run" in view["documents_available_from_extraction"]
