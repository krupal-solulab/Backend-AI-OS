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
from core.common.dtos import AuditEntry, Ctx, WorkflowInput
from core.common.enums import DecisionOutcome, ReviewAction, ReviewStatus, Role, Vertical
from core.config import get_settings
from core.extraction import DefaultExtractionService
from core.llm import build_llm_service
from core.models import Tenant
from core.review_queue import DefaultReviewQueueService
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
