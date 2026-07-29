"""E&S Market Matching eval test — proves the pipeline against the REAL
Workflow_10 dataset (originally ``Data sets/Workflow 1/market_matching_dataset``,
copied to ``TEST_DATA_ROOT/Workflow_10/test_dataset`` per DATA_AND_FIXTURES.md;
see that folder's Validation_Rules_Test_Dataset.md for the expected-outcome
spec this test asserts against, including one documented deviation from the
dataset's own interpretation guide).

Pytest-discovered here (not under ``src/verticals/es/...``) because this
project's ``pyproject.toml`` sets ``testpaths = ["tests"]`` — see
``verticals/es/workflows/market_matching/eval_test.py`` for that note.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from sqlmodel import col, select

import core.models  # noqa: F401  (registers tables)
from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, RawBundle, RawDocument, WorkflowInput
from core.common.enums import DecisionOutcome, DocumentKind, ReviewStatus, Role, Vertical
from core.config import get_settings
from core.documents import LocalDocumentStore
from core.extraction import DefaultExtractionService
from core.ingestion import MockConnectorService
from core.llm import build_llm_service
from core.models import ReviewItem as ReviewItemRow
from core.models import Tenant
from core.review_queue import DefaultReviewQueueService
from core.rules_engine import DefaultRulesEngine
from verticals.es.workflows.market_matching.router import RunRequest, run_market_matching
from verticals.es.workflows.market_matching.service import (
    DEFAULT_WORKFLOW_N,
    MarketMatchingPipeline,
)

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_10 fixtures unavailable",
)


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


@pytest.fixture
def es_ctx() -> Ctx:
    return Ctx(tenant_id="demo-es", vertical=Vertical.ES, user_id="u-jr", role=Role.JUNIOR)


def _pipeline(session: AsyncSession) -> MarketMatchingPipeline:
    return MarketMatchingPipeline(
        session=session,
        connector=MockConnectorService(workflow_n=DEFAULT_WORKFLOW_N),
        extraction=DefaultExtractionService(),
        rules_engine=DefaultRulesEngine(),
        llm=build_llm_service(),
        documents=LocalDocumentStore(),
        workflow_n=DEFAULT_WORKFLOW_N,
    )


async def _decide_for(session: AsyncSession, ctx: Ctx, submission_ref: str):
    pipeline = _pipeline(session)
    inp = WorkflowInput(submission_id=submission_ref, source_ref=submission_ref)
    raw = await pipeline.ingest(ctx, inp)
    model = await pipeline.extract(ctx, raw)
    decision = await pipeline.decide(ctx, model)
    return model, decision


def _match_ids(decision) -> list[str]:
    return [m["carrier_id"] for m in decision.details["matches"]]


def _excluded_ids(decision) -> set[str]:
    return {e["carrier_id"] for e in decision.details["excluded"]}


async def test_ingest_and_extract_real_fixtures(es_session, es_ctx) -> None:
    pipeline = _pipeline(es_session)
    inp = WorkflowInput(submission_id="submission_01", source_ref="submission_01")
    raw = await pipeline.ingest(es_ctx, inp)
    # acord, loss_run, financial_statement, email — MockConnectorService.to_raw_bundle()
    # includes the cover email in `.documents` too (unlike get_attachments(), which
    # filters it out); it's also set separately as `raw.email_body`.
    assert len(raw.documents) == 4

    model = await pipeline.extract(es_ctx, raw)
    extracted = [f for f in model.fields if not f.name.startswith("documents.")]
    assert extracted and all(f.citation is not None for f in extracted)  # every value cited
    assert any(f.name == "acord.class_code" for f in model.fields)


async def test_submission_01_completeness_ranks_not_excludes(es_session, es_ctx) -> None:
    """MM-06: a carrier missing loss-run history is ranked lower with a flag,
    NEVER hard-excluded — this is the specific mistake the interpretation guide
    calls out as the most common wrong implementation."""
    _, decision = await _decide_for(es_session, es_ctx, "submission_01")
    assert decision.outcome is DecisionOutcome.PROCEED

    ids = _match_ids(decision)
    assert ids[0] == "CAR-03"  # Ironclad #1 — its 3yr requirement is already fully met
    assert "CAR-01" in ids  # Meridian NOT hard-excluded despite the loss-run gap
    assert "CAR-02" in ids  # Palmetto NOT hard-excluded despite the loss-run gap

    by_id = {m["carrier_id"]: m for m in decision.details["matches"]}
    assert by_id["CAR-01"]["missing"], "Meridian should carry a missing-loss-history flag"
    assert by_id["CAR-02"]["missing"], "Palmetto should carry a missing-loss-history flag"
    assert not by_id["CAR-03"]["missing"], "Ironclad's requirements are already fully met"


async def test_submission_02_habitational_class_exclusion(es_session, es_ctx) -> None:
    _, decision = await _decide_for(es_session, es_ctx, "submission_02")
    assert _match_ids(decision) == ["CAR-04"]  # Coastal Mutual only
    assert {"CAR-01", "CAR-02", "CAR-03", "CAR-05", "CAR-06"} <= _excluded_ids(decision)


async def test_submission_03_premium_floor(es_session, es_ctx) -> None:
    _, decision = await _decide_for(es_session, es_ctx, "submission_03")
    assert _match_ids(decision) == ["CAR-05"]  # Apex Excess Lines only


async def test_submission_04_premium_ceiling(es_session, es_ctx) -> None:
    _, decision = await _decide_for(es_session, es_ctx, "submission_04")
    ids = _match_ids(decision)
    assert ids[0] == "CAR-06"  # Vantage — the clean, top-ranked match
    assert "CAR-01" not in ids and "CAR-02" not in ids and "CAR-04" not in ids
    # Documented deviation from the interpretation guide's summary-table prose:
    # per the ACTUAL carrier_03_ironclad.json (trucking accepted, premium in-band)
    # there is no hard-exclusion basis for Ironclad here — MM-05 severity is a
    # soft factor for this (non-roofing) class, so it appears, score-penalized,
    # rather than excluded. See Validation_Rules_Test_Dataset.md.
    if "CAR-03" in ids:
        by_id = {m["carrier_id"]: m for m in decision.details["matches"]}
        assert by_id["CAR-03"]["score"] < by_id["CAR-06"]["score"]


async def test_submission_05_roofing_scope_and_severity(es_session, es_ctx) -> None:
    """Tests both class-scope nuance (steep vs. low-slope) and the severity
    ceiling being HARD for this (roofing) class specifically."""
    _, decision = await _decide_for(es_session, es_ctx, "submission_05")
    assert _match_ids(decision) == ["CAR-03"]  # Ironclad only
    excluded = _excluded_ids(decision)
    assert {"CAR-01", "CAR-04"} <= excluded  # Meridian, Coastal Mutual — class
    assert "CAR-02" in excluded  # Palmetto — accepts low-slope only, not steep


async def test_submission_06_zero_match_and_diligent_search(es_session, es_ctx) -> None:
    """The single most important failure mode per the interpretation guide:
    resist forcing a low-confidence match when no real fit exists."""
    _, decision = await _decide_for(es_session, es_ctx, "submission_06")
    assert decision.outcome is DecisionOutcome.DECLINE
    assert decision.details["matches"] == []
    assert len(decision.details["excluded"]) == 6  # every carrier, class-excluded (cannabis)
    # MM-07 fires regardless of the zero-match outcome.
    assert decision.details["diligent_search"]["required"] is True


async def test_missing_acord_yields_request_info(es_session, es_ctx) -> None:
    """Synthetic case — this dataset's 6 real submissions all include an ACORD,
    so this constructs a bundle without one to prove the REQUEST_INFO path:
    no class code / premium means no carrier matching can even be attempted,
    independent of any specific carrier's appetite."""
    pipeline = _pipeline(es_session)
    raw = RawBundle(
        submission_id="synthetic-no-acord",
        documents=[
            RawDocument(
                kind=DocumentKind.LOSS_RUN, filename="loss_run.txt",
                content="Named Insured: Test Co\n",
            )
        ],
    )
    model = await pipeline.extract(es_ctx, raw)
    decision = await pipeline.decide(es_ctx, model)
    assert decision.outcome is DecisionOutcome.REQUEST_INFO


async def test_full_pipeline_draft_review_and_audit(es_session, es_ctx) -> None:
    """ingest -> extract -> decide -> draft -> package -> review queue -> audit,
    end to end, for a real submission with an actual top match."""
    pipeline = _pipeline(es_session)
    output = await pipeline.run(
        es_ctx, WorkflowInput(submission_id="submission_01", source_ref="submission_01")
    )
    assert output.decision.outcome is DecisionOutcome.PROCEED
    assert output.draft is not None and output.draft.text
    assert output.payload["matches"][0]["carrier_id"] == "CAR-03"

    rq = DefaultReviewQueueService()
    item = await rq.enqueue(es_session, es_ctx, output, "market_matching")
    assert item.status is ReviewStatus.PENDING

    audit = DefaultAuditService()
    await audit.record(
        es_session, es_ctx,
        AuditEntry(
            actor="ai", who="system", what=f"decision={output.decision.outcome.value}",
            workflow="market_matching", tenant_id=es_ctx.tenant_id, vertical=es_ctx.vertical,
        ),
    )
    entries = await audit.query(es_session, es_ctx, {"workflow": "market_matching"})
    assert len(entries) == 1
    assert entries[0].actor == "ai"


async def test_zero_match_seeds_diligent_search_stub(es_session, es_ctx) -> None:
    """MM-07 -> Diligent Search (Phase 2 connectivity): a real zero-match
    result whose diligent_search flag is required must seed a real, linked
    Diligent Search review item stub — additive, via /run's router-level
    hook, not a fake determination. Re-running the same submission must
    never create a second stub."""
    await run_market_matching(RunRequest(submission_ref="submission_06"), es_ctx, es_session)

    ds_items = (
        await es_session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.tenant_id) == es_ctx.tenant_id,
                col(ReviewItemRow.workflow) == "diligent_search",
            )
        )
    ).scalars().all()
    assert len(ds_items) == 1

    await run_market_matching(RunRequest(submission_ref="submission_06"), es_ctx, es_session)
    ds_items_again = (
        await es_session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.tenant_id) == es_ctx.tenant_id,
                col(ReviewItemRow.workflow) == "diligent_search",
            )
        )
    ).scalars().all()
    assert len(ds_items_again) == 1
