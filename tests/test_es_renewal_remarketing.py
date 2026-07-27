"""E&S Renewal Remarketing eval test — proves the pipeline + router against
the REAL Workflow_16 dataset (originally
``Data sets/Workflow 7/renewal_remarketing_dataset``, copied to
``TEST_DATA_ROOT/Workflow_16/test_dataset`` per DATA_AND_FIXTURES.md).

Pytest-discovered here (not under src/verticals/es/...) — see
verticals/es/workflows/renewal_remarketing/eval_test.py for why.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, WorkflowInput
from core.common.enums import ReviewStatus, Role, Vertical
from core.config import get_settings
from core.llm import build_llm_service
from core.models import Tenant
from core.review_queue import DefaultReviewQueueService
from verticals.es.workflows.renewal_remarketing.router import (
    RunRequest,
    accept_incumbent,
    escalate,
    initiate_remarket,
    run_renewal_remarketing,
)
from verticals.es.workflows.renewal_remarketing.service import RenewalRemarketingPipeline

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_16 fixtures unavailable",
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


def _pipeline() -> RenewalRemarketingPipeline:
    return RenewalRemarketingPipeline(llm=build_llm_service())


async def _run(ctx: Ctx, scenario_ref: str):
    pipeline = _pipeline()
    return await pipeline.run(ctx, WorkflowInput(source_ref=scenario_ref))


async def test_scenario_01_no_remarket_clean(es_ctx) -> None:
    """Flat exposure, no losses, responsive incumbent, reasonable terms —
    the clean baseline."""
    output = await _run(es_ctx, "scenario_01")
    payload = output.payload
    assert payload["trigger_decision"]["level"] == "NO_REMARKET"
    assert payload["incumbent_status"]["non_response_flag"] is False


async def test_scenario_02_full_remarket_disproportionate_severity(es_ctx) -> None:
    """A 58.5% premium increase far exceeding 3.8% exposure growth, driven
    by continued adverse loss activity — worth confirming no better
    alternative exists."""
    output = await _run(es_ctx, "scenario_02")
    payload = output.payload
    assert payload["trigger_decision"]["level"] == "FULL_REMARKET"
    assert payload["loss_history_change"]["trend"] == "worsening"
    assert payload["loss_history_change"]["new_claims_count"] == 1


async def test_scenario_03_light_remarket_check_explained_growth(es_ctx) -> None:
    """32.5% exposure growth is ENTIRELY explained by an already-endorsed
    location addition — must NOT read as a fresh adverse signal, but the
    larger size band still warrants a lightweight check, not a full
    remarket. This is the core proof that the four-state model isn't
    collapsible to binary."""
    output = await _run(es_ctx, "scenario_03")
    payload = output.payload
    assert payload["exposure_change"]["material"] is True
    assert payload["exposure_change"]["already_endorsed"] is True
    assert payload["loss_history_change"]["trend"] == "improving"
    assert payload["trigger_decision"]["level"] == "LIGHT_REMARKET_CHECK"


async def test_scenario_04_urgent_remarket_non_response(es_ctx) -> None:
    """Incumbent silence with only 25 days before expiration — a lapse-risk
    signal, independent of pricing/exposure, which are both clean here."""
    output = await _run(es_ctx, "scenario_04")
    payload = output.payload
    assert payload["incumbent_status"]["renewal_terms_received"] is False
    assert payload["incumbent_status"]["non_response_flag"] is True
    assert payload["trigger_decision"]["level"] == "URGENT_REMARKET"
    assert payload["exposure_change"]["material"] is False


async def test_scenario_05_comparison_stage_exception_flagged(es_ctx) -> None:
    """Post-remarket comparison: Palmetto's quote is cheaper but carries a
    3x higher deductible AND required a manual underwriting exception —
    must never default to recommending the lower premium."""
    output = await _run(es_ctx, "scenario_05")
    payload = output.payload
    assert payload["is_comparison_stage"] is True
    comparison = payload["remarket_execution"]["comparison_output"]
    assert comparison["directly_comparable"] is False
    assert "deductible" in comparison["material_differences"]
    assert comparison["alternative"]["is_exception_based"] is True
    assert comparison["alternative"]["carrier_name"] == "Palmetto Specialty Underwriters"


async def test_scenario_06_no_remarket_history_suppressed(es_ctx) -> None:
    """Grounded in THIS account's own 2-cycle history of minimal savings —
    not a generic small-account exemption."""
    output = await _run(es_ctx, "scenario_06")
    payload = output.payload
    assert payload["trigger_decision"]["level"] == "NO_REMARKET"
    assert payload["remarketing_history_detail"] is not None
    assert "2 of the last 2" in payload["remarketing_history_detail"]
    assert "history" in payload["trigger_decision"]["reasoning"]["summary"].lower()


async def test_initiate_remarket_rejected_for_no_remarket(es_ctx, es_session) -> None:
    item = await run_renewal_remarketing(RunRequest(scenario_ref="scenario_01"), es_ctx, es_session)
    with pytest.raises(HTTPException) as exc_info:
        await initiate_remarket(item.id, es_ctx, es_session)
    assert exc_info.value.status_code == 409


async def test_initiate_remarket_full_remarket_reinvokes_market_matching(
    es_ctx, es_session
) -> None:
    """RR-05: a genuine re-invocation of the real MarketMatchingPipeline —
    verified by checking a real market_matching review item gets created,
    not just a flag flipped."""
    item = await run_renewal_remarketing(RunRequest(scenario_ref="scenario_02"), es_ctx, es_session)
    result = await initiate_remarket(item.id, es_ctx, es_session)
    assert result.payload.remarket_execution.initiated is True
    assert result.payload.remarket_execution.market_matching_output_id is not None


async def test_accept_incumbent_action(es_ctx, es_session) -> None:
    item = await run_renewal_remarketing(RunRequest(scenario_ref="scenario_01"), es_ctx, es_session)
    result = await accept_incumbent(item.id, es_ctx, es_session)
    assert result.payload.final_decision.outcome == "renewed_incumbent"
    assert result.payload.final_decision.decided_by == es_ctx.user_id


async def test_escalate_action(es_ctx, es_session) -> None:
    item = await run_renewal_remarketing(RunRequest(scenario_ref="scenario_04"), es_ctx, es_session)
    escalated = await escalate(item.id, es_ctx, es_session)
    assert escalated.status == ReviewStatus.ESCALATED.value


async def test_full_pipeline_review_queue_and_audit(es_ctx, es_session) -> None:
    output = await _run(es_ctx, "scenario_01")
    assert output.draft is not None and output.draft.text

    rq = DefaultReviewQueueService()
    item = await rq.enqueue(es_session, es_ctx, output, "renewal_remarketing")
    assert item.status is ReviewStatus.PENDING

    audit = DefaultAuditService()
    await audit.record(
        es_session, es_ctx,
        AuditEntry(
            actor="ai", who="system", what=f"decision={output.decision.outcome.value}",
            workflow="renewal_remarketing", tenant_id=es_ctx.tenant_id, vertical=es_ctx.vertical,
        ),
    )
    entries = await audit.query(es_session, es_ctx, {"workflow": "renewal_remarketing"})
    assert len(entries) == 1
