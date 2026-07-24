"""E&S Retail Agent Communication eval test — proves the pipeline + router
against the REAL Workflow_12 dataset (originally
``Data sets/Workflow 3/retail_comm_dataset``, copied to
``TEST_DATA_ROOT/Workflow_12/test_dataset`` per DATA_AND_FIXTURES.md; see that
file's Workflow_12 layout note). ``expected_draft.txt``/``tone_notes.txt`` are
illustrative reference material for a human reviewer, not literal strings
asserted against verbatim (LLM/mock-LLM phrasing varies) — these tests assert
structural/behavioral properties instead: correct trigger classification,
correct per-carrier scoping, the compliance gate, and that the grounded facts
actually reach the draft.

Pytest-discovered here (not under src/verticals/es/...) — see
verticals/es/workflows/agent_communication/eval_test.py for why.
"""

from __future__ import annotations

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
from verticals.es.workflows.agent_communication.router import (
    RunRequest,
    approve,
    compliance_clear,
    run_agent_communication,
)
from verticals.es.workflows.agent_communication.service import (
    DEFAULT_WORKFLOW_N,
    AgentCommunicationPipeline,
)
from verticals.es.workflows.agent_communication.trigger_loader import load_trigger

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_12 fixtures unavailable",
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


def _trigger(ref: str) -> dict:
    return load_trigger(DEFAULT_WORKFLOW_N, ref).data


async def _run(session: AsyncSession, ctx: Ctx, trigger_data: dict):
    pipeline = AgentCommunicationPipeline(session=session, llm=build_llm_service())
    inp = WorkflowInput(
        submission_id=trigger_data.get("submission_id"), params={"trigger_data": trigger_data}
    )
    return await pipeline.run(ctx, inp)


async def test_trigger_01_submission_acknowledgment_combines_both_carriers(
    es_ctx, es_session
) -> None:
    """FR-4: one draft covers BOTH carriers approached for this submission,
    rather than fragmenting into separate emails."""
    output = await _run(es_session, es_ctx, _trigger("trigger_01"))
    assert output.decision.outcome is DecisionOutcome.PROCEED
    assert output.payload["trigger_type"] == "SUBMISSION_ACKNOWLEDGMENT"
    assert output.payload["requires_compliance_review"] is False
    assert output.payload["carrier_names_disclosed"] is True
    assert "Ironclad Casualty Solutions" in output.draft.text
    assert "Meridian Excess & Surplus" in output.draft.text


async def test_trigger_02_missing_info_scoped_to_carrier(es_ctx, es_session) -> None:
    """Must scope the ask to Palmetto specifically (FR-1's carrier-specific
    scoping requirement, extending Package Assembly's PA-01 per-carrier
    independence into agent-facing language)."""
    output = await _run(es_session, es_ctx, _trigger("trigger_02"))
    assert output.payload["trigger_type"] == "MISSING_INFO_REQUEST"
    assert output.payload["carrier_name"] == "Palmetto Specialty Underwriters"
    assert output.payload["requires_compliance_review"] is False
    assert "Loss run only covers 3 of 5 years Palmetto requires" in output.draft.text
    assert "Supplemental contractor questionnaire not provided" in output.draft.text


async def test_trigger_03_no_market_found_compliance_gated_no_carrier_names(
    es_ctx, es_session
) -> None:
    """RA-TN-06: highest-sensitivity draft in the set — must be held for
    compliance review and must never name a specific carrier (the source data
    itself carries no per-carrier breakdown for this trigger type)."""
    output = await _run(es_session, es_ctx, _trigger("trigger_03"))
    assert output.payload["trigger_type"] == "NO_MARKET_FOUND"
    assert output.payload["requires_compliance_review"] is True
    assert output.payload["carrier_names_disclosed"] is False
    assert output.payload["status"] == "UNDER_COMPLIANCE_REVIEW"
    assert "cannabis" in output.draft.text.lower()
    for carrier in ("Ironclad", "Meridian", "Palmetto", "Coastal"):
        assert carrier not in output.draft.text


async def test_trigger_04_placement_confirmation_clean(es_ctx, es_session) -> None:
    output = await _run(es_session, es_ctx, _trigger("trigger_04"))
    assert output.payload["trigger_type"] == "PLACEMENT_CONFIRMATION"
    assert output.payload["carrier_name"] == "Coastal Mutual Specialty"
    assert output.payload["requires_compliance_review"] is False
    assert "$81,500" in output.draft.text


async def test_trigger_05_no_response_followup_fallback_subject(es_ctx, es_session) -> None:
    """No prior draft exists in this fresh session's history — must fall back
    to a deterministic 'Re: ...' reconstruction rather than error out."""
    output = await _run(es_session, es_ctx, _trigger("trigger_05"))
    assert output.payload["trigger_type"] == "NO_RESPONSE_FOLLOWUP"
    assert output.payload["subject_line"].startswith("Re:")
    assert "45" in output.draft.text  # carrier_acceptance_window_days
    assert "36" in output.draft.text  # days_remaining_in_window


async def test_trigger_05_no_response_followup_reuses_original_thread_subject(
    es_ctx, es_session
) -> None:
    """FR-10: when the original MISSING_INFO_REQUEST draft (trigger_02, same
    submission_id + carrier) actually exists in this workflow's history, the
    follow-up must reuse ITS subject line, not the deterministic fallback."""
    original = await _run(es_session, es_ctx, _trigger("trigger_02"))
    rq = DefaultReviewQueueService()
    await rq.enqueue(es_session, es_ctx, original, "agent_communication")

    followup = await _run(es_session, es_ctx, _trigger("trigger_05"))
    expected_subject = f"Re: {original.payload['subject_line']}"
    assert followup.payload["subject_line"] == expected_subject


async def test_trigger_06_quote_terms_summary_price_justification(es_ctx, es_session) -> None:
    """RA-TN-08: the hardest pricing case — price must be connected to the
    specific loss-history cause, never presented bare."""
    output = await _run(es_session, es_ctx, _trigger("trigger_06"))
    assert output.payload["trigger_type"] == "QUOTE_TERMS_SUMMARY"
    assert output.payload["carrier_name"] == "Ironclad Casualty Solutions"
    assert output.payload["requires_compliance_review"] is False
    assert "$118,000" in output.draft.text
    assert "180,000" in output.draft.text  # the specific reserved claim driving price


async def test_fr5_no_duplicate_draft_for_unresolved_trigger(es_ctx, es_session) -> None:
    first = await run_agent_communication(
        RunRequest(trigger=_trigger("trigger_01")), es_ctx, es_session
    )
    assert first.deduplicated is False

    second = await run_agent_communication(
        RunRequest(trigger=_trigger("trigger_01")), es_ctx, es_session
    )
    assert second.deduplicated is True
    assert second.id == first.id


async def test_fr12_max_one_followup_per_original_request(es_ctx, es_session) -> None:
    first = await run_agent_communication(
        RunRequest(trigger=_trigger("trigger_05")), es_ctx, es_session
    )
    assert first.deduplicated is False

    with pytest.raises(HTTPException) as exc_info:
        await run_agent_communication(
            RunRequest(trigger=_trigger("trigger_05")), es_ctx, es_session
        )
    assert exc_info.value.status_code == 409


async def test_compliance_gate_blocks_approve_until_cleared(
    es_ctx, es_ctx_senior, es_session
) -> None:
    item = await run_agent_communication(
        RunRequest(trigger=_trigger("trigger_03")), es_ctx, es_session
    )
    assert item.payload is not None and item.payload.requires_compliance_review is True

    with pytest.raises(HTTPException) as exc_info:
        await approve(item.id, es_ctx, es_session)
    assert exc_info.value.status_code == 409

    with pytest.raises(HTTPException) as exc_info:
        await compliance_clear(item.id, es_ctx, es_session)  # junior may not clear
    assert exc_info.value.status_code == 403

    cleared = await compliance_clear(item.id, es_ctx_senior, es_session)
    assert cleared.payload is not None and cleared.payload.requires_compliance_review is False

    approved = await approve(item.id, es_ctx, es_session)
    assert approved.status == ReviewStatus.APPROVED.value


async def test_full_pipeline_review_queue_and_audit(es_ctx, es_session) -> None:
    """package() -> review_queue.enqueue() -> act() -> audit, end to end, for
    one clean trigger."""
    output = await _run(es_session, es_ctx, _trigger("trigger_04"))
    assert output.draft is not None and output.draft.text

    rq = DefaultReviewQueueService()
    item = await rq.enqueue(es_session, es_ctx, output, "agent_communication")
    assert item.status is ReviewStatus.PENDING

    acted = await rq.act(es_session, es_ctx, item.id, ReviewAction.APPROVE)
    assert acted.status is ReviewStatus.APPROVED

    audit = DefaultAuditService()
    await audit.record(
        es_session, es_ctx,
        AuditEntry(
            actor="ai", who="system", what=f"decision={output.decision.outcome.value}",
            workflow="agent_communication", tenant_id=es_ctx.tenant_id, vertical=es_ctx.vertical,
        ),
    )
    entries = await audit.query(es_session, es_ctx, {"workflow": "agent_communication"})
    assert len(entries) == 1
