"""E&S Endorsement / Mid-Term Change Processing eval test — proves the
pipeline + router against the REAL Workflow_15 dataset (originally
``Data sets/Workflow 6/endorsement_dataset``, copied to
``TEST_DATA_ROOT/Workflow_15/test_dataset`` per DATA_AND_FIXTURES.md; see
that file's Workflow_15 layout note for the mixed pre-issuance/
reconciliation fixture shape).

Pytest-discovered here (not under src/verticals/es/...) — see
verticals/es/workflows/endorsement/eval_test.py for why.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, WorkflowInput
from core.common.enums import DecisionOutcome, ReviewStatus, Role, Vertical
from core.config import get_settings
from core.llm import build_llm_service
from core.models import Tenant
from core.review_queue import DefaultReviewQueueService
from verticals.es.workflows.agent_communication.router import list_agent_communication
from verticals.es.workflows.endorsement.router import (
    ResolveDiscrepancyRequest,
    RunRequest,
    escalate,
    resolve_discrepancy,
    run_endorsement,
    send,
)
from verticals.es.workflows.endorsement.service import EndorsementPipeline

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_15 fixtures unavailable",
)


@pytest.fixture
def es_ctx() -> Ctx:
    return Ctx(tenant_id="demo-es", vertical=Vertical.ES, user_id="u-jr", role=Role.JUNIOR)


@pytest.fixture
def es_ctx_senior() -> Ctx:
    return Ctx(tenant_id="demo-es", vertical=Vertical.ES, user_id="u-sr", role=Role.SENIOR)


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


def _pipeline() -> EndorsementPipeline:
    return EndorsementPipeline(llm=build_llm_service())


async def _run(ctx: Ctx, scenario_ref: str):
    pipeline = _pipeline()
    return await pipeline.run(ctx, WorkflowInput(source_ref=scenario_ref))


async def test_scenario_01_routine_additional_insured(es_ctx) -> None:
    output = await _run(es_ctx, "scenario_01")
    payload = output.payload
    assert output.decision.outcome is DecisionOutcome.PROCEED
    assert payload["classification"] == "ROUTINE"
    assert payload["appetite_recheck"]["applicable"] is False
    assert payload["appetite_recheck"]["outcome"] == "NOT_APPLICABLE"
    assert payload["premium_impact"]["premium_bearing"] is False


async def test_scenario_02_underwriting_review_limit_increase(es_ctx) -> None:
    """A 2x limit increase can never be routine, regardless of appetite —
    but the appetite recheck still runs (severity exposure touch)."""
    output = await _run(es_ctx, "scenario_02")
    payload = output.payload
    assert payload["classification"] == "UNDERWRITING_REVIEW_REQUIRED"
    assert payload["appetite_recheck"]["applicable"] is True
    assert payload["appetite_recheck"]["outcome"] == "WITHIN_APPETITE"
    assert "180,000" in payload["appetite_recheck"]["detail"] or "loss" in (
        payload["appetite_recheck"]["detail"].lower()
    )
    assert payload["premium_impact"]["premium_bearing"] is True


async def test_scenario_03_appetite_unknown_operations_class(es_ctx) -> None:
    """The single most important judgment call in this workflow: solar
    panel installation is on NEITHER Ironclad's accepted nor excluded
    list — must surface as a genuine unknown, never auto-approved or
    auto-rejected."""
    output = await _run(es_ctx, "scenario_03")
    payload = output.payload
    assert output.decision.outcome is DecisionOutcome.REQUEST_INFO
    assert payload["classification"] == "UNDERWRITING_REVIEW_REQUIRED"
    assert payload["appetite_recheck"]["applicable"] is True
    assert payload["appetite_recheck"]["outcome"] == "APPETITE_UNKNOWN"


async def test_scenario_04_material_but_in_appetite_location(es_ctx) -> None:
    """A large new location, but a class the carrier already knows and
    wants — distinct from Scenario 03's appetite-unknown case. Also tests
    the missing-state-licensing clarification flag and pro-rata inputs,
    falling back to the REAL Workflow_10 carrier profile since this
    scenario's fixture doesn't embed accepted/excluded lists directly."""
    output = await _run(es_ctx, "scenario_04")
    payload = output.payload
    assert payload["classification"] == "UNDERWRITING_REVIEW_REQUIRED"
    assert payload["appetite_recheck"]["outcome"] == "WITHIN_APPETITE"
    assert payload["appetite_recheck"]["state_licensing_clarification_needed"] is True
    assert payload["premium_impact"]["premium_bearing"] is True
    proration = payload["premium_impact"]["proration_inputs"]
    assert proration["days_elapsed"] == 47
    assert proration["days_remaining"] == 319
    assert proration["term_total_days"] == 366


async def test_scenario_05_partial_reconciliation_discrepancy(es_ctx, es_session) -> None:
    """The carrier only processed one of two requested additional
    insureds — must flag as a partial discrepancy, never treated as fully
    reconciled, and hold the endorsement-confirmed trigger."""
    output = await _run(es_ctx, "scenario_05")
    payload = output.payload
    assert payload["requested_items"] == ["Midstate Distribution Co.", "Harborline Logistics"]
    assert payload["carrier_response"]["issued_items"] == ["Midstate Distribution Co."]
    assert payload["carrier_response"]["reconciliation_status"] == "DISCREPANCY_FLAGGED"
    missing = [d["requested_item"] for d in payload["carrier_response"]["discrepancy_detail"]]
    assert missing == ["Harborline Logistics"]
    assert payload["downstream_trigger_fired"] is False

    rq = DefaultReviewQueueService()
    item = await rq.enqueue(es_session, es_ctx, output, "endorsement")
    before = await list_agent_communication(es_ctx, es_session)

    resolved = await resolve_discrepancy(
        item.id, ResolveDiscrepancyRequest(resolution="flag_carrier_error"), es_ctx, es_session
    )
    assert resolved.payload.carrier_response.reconciliation_status == "BROKER_RESOLVED"
    assert resolved.payload.downstream_trigger_fired is True

    after = await list_agent_communication(es_ctx, es_session)
    assert len(after) == len(before) + 1


async def test_scenario_06_routine_headcount_control_case(es_ctx) -> None:
    """A 75% headcount increase looks material by percentage, but the
    account's small absolute premium keeps it routine — the control case
    proving percentage isn't assessed alone. Also a "nothing unusual"
    proration control case."""
    output = await _run(es_ctx, "scenario_06")
    payload = output.payload
    assert payload["classification"] == "ROUTINE"
    assert payload["premium_impact"]["premium_bearing"] is True
    proration = payload["premium_impact"]["proration_inputs"]
    assert proration["days_remaining"] == 309
    assert proration["term_total_days"] == 366
    assert proration["unusual_timing_flag"] is None


def test_ep06_unusual_timing_flag_synthetic() -> None:
    """EP-06/FR-10: unusual timing must be flagged — out-of-term (before
    effective date or after expiration) and too-close-to-expiration cases.
    None of the 6 real scenarios exercise this, so synthetic dates are used
    here, kept entirely separate from the scenario-based tests above."""
    from datetime import date

    from verticals.es.workflows.endorsement.classification_engine import proration_inputs

    effective = date(2027, 1, 1)
    expiration = date(2028, 1, 1)  # 365-day term

    normal = proration_inputs(effective, expiration, date(2027, 6, 1))
    assert normal.unusual_timing_flag is None

    before_term = proration_inputs(effective, expiration, date(2026, 12, 1))
    assert before_term.days_elapsed < 0
    assert "before the policy's own effective date" in before_term.unusual_timing_flag

    after_term = proration_inputs(effective, expiration, date(2028, 2, 1))
    assert after_term.days_remaining < 0
    assert "would need to be renewed first" in after_term.unusual_timing_flag

    near_expiration = proration_inputs(effective, expiration, date(2027, 12, 20))
    assert 0 <= near_expiration.days_remaining < 30
    assert "close to expiration" in near_expiration.unusual_timing_flag


async def test_send_and_escalate_actions(es_ctx, es_ctx_senior, es_session) -> None:
    item = await run_endorsement(RunRequest(scenario_ref="scenario_01"), es_ctx, es_session)
    # SEND is senior/admin-only project-wide (core.review_queue's frozen rule).
    sent = await send(item.id, es_ctx_senior, es_session)
    assert sent.status == ReviewStatus.SENT.value

    item2 = await run_endorsement(RunRequest(scenario_ref="scenario_03"), es_ctx, es_session)
    escalated = await escalate(item2.id, es_ctx, es_session)
    assert escalated.status == ReviewStatus.ESCALATED.value


async def test_invalid_resolution_value_rejected(es_ctx, es_session) -> None:
    item = await run_endorsement(RunRequest(scenario_ref="scenario_05"), es_ctx, es_session)
    with pytest.raises(HTTPException) as exc_info:
        await resolve_discrepancy(
            item.id, ResolveDiscrepancyRequest(resolution="not_a_real_option"), es_ctx, es_session
        )
    assert exc_info.value.status_code == 422


async def test_full_pipeline_review_queue_and_audit(es_ctx, es_session) -> None:
    output = await _run(es_ctx, "scenario_01")
    assert output.draft is not None and output.draft.text

    rq = DefaultReviewQueueService()
    item = await rq.enqueue(es_session, es_ctx, output, "endorsement")
    assert item.status is ReviewStatus.PENDING

    audit = DefaultAuditService()
    await audit.record(
        es_session, es_ctx,
        AuditEntry(
            actor="ai", who="system", what=f"decision={output.decision.outcome.value}",
            workflow="endorsement", tenant_id=es_ctx.tenant_id, vertical=es_ctx.vertical,
        ),
    )
    entries = await audit.query(es_session, es_ctx, {"workflow": "endorsement"})
    assert len(entries) == 1
