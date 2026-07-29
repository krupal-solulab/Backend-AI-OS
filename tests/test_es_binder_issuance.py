"""E&S Binder & Policy Issuance Coordination eval test — proves the pipeline
+ router against the REAL Workflow_14 dataset (originally
``Data sets/Workflow 5/binder_issuance_dataset``, copied to
``TEST_DATA_ROOT/Workflow_14/test_dataset`` per DATA_AND_FIXTURES.md; see
that file's Workflow_14 layout note for the mixed pre-bind/post-issuance
fixture shape).

Pytest-discovered here (not under src/verticals/es/...) — see
verticals/es/workflows/binder_issuance/eval_test.py for why.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, WorkflowInput
from core.common.enums import DecisionOutcome, ReviewAction, ReviewStatus, Role, Vertical
from core.config import get_settings
from core.llm import build_llm_service
from core.models import Tenant
from core.review_queue import DefaultReviewQueueService
from verticals.es.workflows.agent_communication.router import list_agent_communication
from verticals.es.workflows.binder_issuance.coordination_engine import recompute_live_state
from verticals.es.workflows.binder_issuance.router import (
    ResolveDiscrepancyRequest,
    RunFromQuoteRequest,
    RunRequest,
    escalate,
    resolve_confirmation_discrepancy,
    resolve_policy_discrepancy,
    run_binder_issuance,
    run_binder_issuance_from_quote,
)
from verticals.es.workflows.binder_issuance.service import BinderIssuancePipeline
from verticals.es.workflows.quote_comparison.router import (
    RunRequest as QuoteRunRequest,
)
from verticals.es.workflows.quote_comparison.router import (
    run_quote_comparison,
    select_quote,
)

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_14 fixtures unavailable",
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


def _pipeline() -> BinderIssuancePipeline:
    return BinderIssuancePipeline(llm=build_llm_service())


async def _run(ctx: Ctx, scenario_ref: str, as_of: str):
    pipeline = _pipeline()
    return await pipeline.run(ctx, WorkflowInput(source_ref=scenario_ref, params={"as_of": as_of}))


async def test_scenario_01_clean_bind_fires_placement_confirmation(es_ctx) -> None:
    """Clean confirmation, monitoring initiated against the carrier's
    "30-45 days" stated timeline (upper bound used) — matches the dataset's
    own worked expected_by (~10/04/2027)."""
    output = await _run(es_ctx, "scenario_01", as_of="2027-08-25")
    payload = output.payload
    assert payload["bind_order_status"] == "SENT"
    assert payload["carrier_confirmation"]["reconciliation_status"] == "CLEAN"
    assert payload["policy_issuance"]["expected_by_date"] == "2027-10-04"
    assert payload["policy_issuance"]["timeline_is_assumed_default"] is False
    assert payload["downstream_triggers_fired"]["placement_confirmation"] is True


async def test_scenario_02_blocked_by_material_pre_bind_subjectivity(es_ctx) -> None:
    """The loss control inspection is material + pre-bind + not cleared —
    must BLOCK, never reach confirmation reconciliation at all."""
    output = await _run(es_ctx, "scenario_02", as_of="2027-07-20")
    payload = output.payload
    assert payload["bind_order_status"] == "BLOCKED"
    assert payload["carrier_confirmation"]["reconciliation_status"] == "PENDING"
    assert payload["downstream_triggers_fired"]["placement_confirmation"] is False
    blocking = [s for s in payload["pre_bind_subjectivities"] if s["status"] == "open"]
    assert len(blocking) == 1
    assert "inspection" in blocking[0]["description"].lower()


async def test_scenario_03_confirmation_discrepancy_suppresses_trigger(es_ctx, es_session) -> None:
    """The single most important test case: a binder number does NOT mean
    clean — deductible and effective date both mismatch and must flag,
    holding Placement Confirmation until the broker resolves it."""
    output = await _run(es_ctx, "scenario_03", as_of="2027-08-26")
    payload = output.payload
    assert payload["bind_order_status"] == "SENT"
    assert payload["carrier_confirmation"]["reconciliation_status"] == "DISCREPANCY_FLAGGED"
    fields = {d["field"] for d in payload["carrier_confirmation"]["discrepancy_detail"]}
    assert "deductible (all perils)" in fields
    assert "effective_date" in fields
    assert payload["downstream_triggers_fired"]["placement_confirmation"] is False

    # End-to-end: enqueue, then resolve via the router action -> trigger releases.
    rq = DefaultReviewQueueService()
    item = await rq.enqueue(es_session, es_ctx, output, "binder_issuance")
    before = await list_agent_communication(es_ctx, es_session)

    resolved = await resolve_confirmation_discrepancy(
        item.id, ResolveDiscrepancyRequest(resolution="accept_carrier_version"), es_ctx, es_session
    )
    assert resolved.payload.carrier_confirmation.reconciliation_status == "BROKER_RESOLVED"
    assert resolved.payload.downstream_triggers_fired.placement_confirmation is True

    after = await list_agent_communication(es_ctx, es_session)
    assert len(after) == len(before) + 1


async def test_scenario_04_clean_bind_with_post_bind_ongoing_obligation(es_ctx) -> None:
    """Same underlying requirement type as Scenario 02 (a loss control
    item) but classified POST_BIND_ONGOING here — must NOT block, and must
    be tracked as a dated task, not dropped."""
    output = await _run(es_ctx, "scenario_04", as_of="2027-08-01")
    payload = output.payload
    assert payload["bind_order_status"] == "SENT"
    assert payload["carrier_confirmation"]["reconciliation_status"] == "CLEAN"
    assert payload["downstream_triggers_fired"]["placement_confirmation"] is True

    obligations = payload["post_bind_ongoing_obligations"]
    assert len(obligations) == 1
    assert obligations[0]["due_date"] == "2027-09-29"
    assert obligations[0]["status"] == "open"


async def test_scenario_04_reminder_due_via_read_time_recompute(es_ctx) -> None:
    """FR-17: a reminder becomes due as the deadline approaches — recomputed
    live, not frozen from ingestion time."""
    output = await _run(es_ctx, "scenario_04", as_of="2027-08-01")
    not_yet = recompute_live_state(output.payload, date(2027, 8, 15))
    assert not_yet["post_bind_ongoing_obligations"][0]["reminder_due"] is False

    now_due = recompute_live_state(output.payload, date(2027, 9, 25))  # 4 days before 09/29
    assert now_due["post_bind_ongoing_obligations"][0]["reminder_due"] is True


async def test_scenario_05_overdue_issuance_alert(es_ctx) -> None:
    """Per the scenario's own bind_record.json ("current_date_at_check":
    "2027-09-25"): 21 days overdue against the carrier's own stated 30-day
    timeline, no documents received — must proactively alert."""
    output = await _run(es_ctx, "scenario_05", as_of="2027-09-25")
    payload = output.payload
    assert payload["policy_issuance"]["expected_by_date"] == "2027-09-04"
    assert payload["policy_issuance"]["documents_received"] is False
    assert payload["policy_issuance"]["overdue_alert_fired"] is True


async def test_scenario_06_issued_policy_material_discrepancy(es_ctx, es_session) -> None:
    """The highest-value check in the whole workflow: every field matches
    except wind/hail deductible (doubled) — must flag as a MATERIAL
    discrepancy and hold Policy Documents Delivered, field-by-field, not a
    holistic 'looks about right' comparison."""
    output = await _run(es_ctx, "scenario_06", as_of="2027-08-20")
    payload = output.payload
    assert payload["issued_policy_reconciliation"]["status"] == "POLICY_DISCREPANCY_FLAGGED"
    fields = {d["field"] for d in payload["issued_policy_reconciliation"]["discrepancy_detail"]}
    assert fields == {"deductible (wind/hail)"}
    detail = next(
        d for d in payload["issued_policy_reconciliation"]["discrepancy_detail"]
        if d["field"] == "deductible (wind/hail)"
    )
    assert detail["requested_or_bound"] == "$50,000"
    assert detail["confirmed_or_issued"] == "$100,000"
    assert payload["downstream_triggers_fired"]["policy_documents_delivered"] is False

    rq = DefaultReviewQueueService()
    item = await rq.enqueue(es_session, es_ctx, output, "binder_issuance")
    before = await list_agent_communication(es_ctx, es_session)

    resolved = await resolve_policy_discrepancy(
        item.id, ResolveDiscrepancyRequest(resolution="flag_carrier_error"), es_ctx, es_session
    )
    assert resolved.payload.issued_policy_reconciliation.status == "BROKER_RESOLVED"
    assert resolved.payload.downstream_triggers_fired.policy_documents_delivered is True

    after = await list_agent_communication(es_ctx, es_session)
    assert len(after) == len(before) + 1


async def test_invalid_resolution_value_rejected(es_ctx, es_session) -> None:
    item = await run_binder_issuance(
        RunRequest(scenario_ref="scenario_03", as_of="2027-08-26"), es_ctx, es_session
    )
    with pytest.raises(HTTPException) as exc_info:
        await resolve_confirmation_discrepancy(
            item.id, ResolveDiscrepancyRequest(resolution="not_a_real_option"), es_ctx, es_session
        )
    assert exc_info.value.status_code == 422


async def test_escalate_action(es_ctx, es_session) -> None:
    item = await run_binder_issuance(
        RunRequest(scenario_ref="scenario_03", as_of="2027-08-26"), es_ctx, es_session
    )
    escalated = await escalate(item.id, es_ctx, es_session)
    assert escalated.status == ReviewStatus.ESCALATED.value


async def test_full_pipeline_review_queue_and_audit(es_ctx, es_session) -> None:
    output = await _run(es_ctx, "scenario_01", as_of="2027-08-25")
    assert output.decision.outcome is DecisionOutcome.PROCEED
    assert output.draft is not None and output.draft.text

    rq = DefaultReviewQueueService()
    item = await rq.enqueue(es_session, es_ctx, output, "binder_issuance")
    assert item.status is ReviewStatus.PENDING

    acted = await rq.act(es_session, es_ctx, item.id, ReviewAction.APPROVE)
    assert acted.status is ReviewStatus.APPROVED

    audit = DefaultAuditService()
    await audit.record(
        es_session, es_ctx,
        AuditEntry(
            actor="ai", who="system", what=f"decision={output.decision.outcome.value}",
            workflow="binder_issuance", tenant_id=es_ctx.tenant_id, vertical=es_ctx.vertical,
        ),
    )
    entries = await audit.query(es_session, es_ctx, {"workflow": "binder_issuance"})
    assert len(entries) == 1


async def test_run_from_quote_comparison_uses_real_selected_terms(es_ctx, es_session) -> None:
    """Quote Comparison -> Binder & Policy Issuance (Phase 2 connectivity):
    a real pre-bind pass built from an ACTUAL, just-selected Quote
    Comparison quote (Meridian's real scenario_01 offer), not the
    Workflow_14 fixture — including carrier_id resolved from the real
    Carrier Appetite Profile panel (Quote Comparison itself never sets
    carrier_id)."""
    qc_item = await run_quote_comparison(
        QuoteRunRequest(scenario_ref="scenario_01", as_of="2027-07-29"), es_ctx, es_session
    )
    meridian_quote = next(
        q for q in qc_item.payload.quotes if q.carrier_name == "Meridian Excess & Surplus"
    )
    await select_quote(qc_item.id, meridian_quote.quote_id, es_ctx, es_session)

    items = await run_binder_issuance_from_quote(
        RunFromQuoteRequest(quote_comparison_item_id=qc_item.id), es_ctx, es_session
    )
    payload = items.payload
    assert payload.carrier_name == "Meridian Excess & Surplus"
    assert payload.carrier_id == "CAR-01"  # resolved via the real carrier panel, by name
    assert payload.requested_bind_terms.premium == 24900.0
    assert payload.requested_bind_terms.deductible_all_perils == 2500.0
    assert payload.requested_bind_terms.effective_date == "2027-09-01"
    # Meridian's one material subjectivity (remaining loss history) carries
    # over as still-open, pre-bind — the only honest values at this point.
    material = [s for s in payload.pre_bind_subjectivities if s.materiality == "material"]
    assert material and all(
        s.status == "open" and s.lifecycle_stage == "PRE_BIND" for s in material
    )
    # That same material subjectivity blocks the bind order (BI-02) —
    # correct, real behavior, not a live-path defect.
    assert payload.bind_order_status == "BLOCKED"


async def test_run_from_quote_comparison_requires_a_selection_first(es_ctx, es_session) -> None:
    qc_item = await run_quote_comparison(
        QuoteRunRequest(scenario_ref="scenario_01", as_of="2027-07-29"), es_ctx, es_session
    )
    with pytest.raises(HTTPException) as exc_info:
        await run_binder_issuance_from_quote(
            RunFromQuoteRequest(quote_comparison_item_id=qc_item.id), es_ctx, es_session
        )
    assert exc_info.value.status_code == 409
