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

import base64

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, WorkflowInput
from core.common.enums import DecisionOutcome, ReviewStatus, Role, Vertical
from core.config import get_settings
from core.integrations.repository import upsert_connection
from core.llm import build_llm_service
from core.models import Tenant
from core.review_queue import DefaultReviewQueueService
from verticals.es.workflows.agent_communication.router import list_agent_communication
from verticals.es.workflows.binder_issuance.router import (
    RunFromQuoteRequest,
    run_binder_issuance_from_quote,
)
from verticals.es.workflows.binder_issuance.router import (
    attach_live_confirmation as attach_live_bind_confirmation,
)
from verticals.es.workflows.binder_issuance.router import (
    AttachLiveMessageRequest as AttachLiveBindConfirmationRequest,
)
from verticals.es.workflows.endorsement.router import (
    AttachLiveMessageRequest,
    ResolveDiscrepancyRequest,
    RunLiveFromBinderRequest,
    RunRequest,
    attach_live_issued_endorsement,
    escalate,
    list_live_inbox,
    resolve_discrepancy,
    run_endorsement,
    run_endorsement_from_binder,
    send,
)
from verticals.es.workflows.endorsement.service import EndorsementPipeline
from verticals.es.workflows.quote_comparison.router import (
    RunRequest as QuoteRunRequest,
)
from verticals.es.workflows.quote_comparison.router import (
    run_quote_comparison,
    select_quote,
)

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


# --- Live path: start from a real SENT bind, attach a real issued endorsement -----
#
# Unlike the scenario_* tests above (static Workflow_15 fixture), these prove
# the NEW live path added this session: a real endorsement request started
# from an ACTUAL, already-SENT Binder & Issuance bind (chained from a real
# Quote Comparison selection), with the change type/detail broker-supplied
# (never inferred from raw email text — see live_ingestion.py's module
# docstring), then a REAL carrier-issued-endorsement email attached — same
# mocked-Nango-proxy technique as this session's Quote Comparison/Binder &
# Issuance live tests.

_LIVE_NAMED_INSURED = "Delta Electric Services LLC"
_LIVE_SUBJECT = f"RE: {_LIVE_NAMED_INSURED} - Endorsement"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _bind_confirmation_gmail_handler(request: httpx.Request) -> httpx.Response:
    body = (
        f"We're pleased to confirm your bind on {_LIVE_NAMED_INSURED}.\n\n"
        "Binder Number: IRO-2027-TEST\n"
        "Premium: $26,500\n"
        "General Liability: $1,000,000 / $2,000,000 aggregate\n"
        "Deductible: $2,500\n"
        "Effective Date: 09/01/2027\n"
    )
    path = request.url.path
    if path == "/proxy/gmail/v1/users/me/messages" and request.method == "GET":
        return httpx.Response(200, json={"messages": [{"id": "msg-bind-confirmation-1"}]})
    if path == "/proxy/gmail/v1/users/me/messages/msg-bind-confirmation-1":
        if request.url.params.get("format") == "metadata":
            return httpx.Response(
                200, json={"payload": {"headers": [{"name": "Subject", "value": "BOUND"}]}}
            )
        return httpx.Response(
            200,
            json={
                "payload": {
                    "headers": [
                        {"name": "From", "value": "uw@ironcladcasualty.com"},
                        {"name": "Subject", "value": "BOUND"},
                        {"name": "Date", "value": "Fri, 30 Jul 2027 16:00:00 -0500"},
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": _b64url(body.encode("utf-8"))},
                }
            },
        )
    return httpx.Response(404, json={"error": f"unhandled {request.method} {path}"})


def _issued_endorsement_gmail_handler(*, items: list[str]):
    body = (
        f"Endorsement issued for {_LIVE_NAMED_INSURED}.\n\n"
        "Endorsement Number: END-2027-TEST\n"
        + "".join(f"Added as scheduled additional insured: {item}\n" for item in items)
        + "Effective Date: 09/01/2027\n"
        "No additional premium.\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/proxy/gmail/v1/users/me/messages" and request.method == "GET":
            return httpx.Response(200, json={"messages": [{"id": "msg-issued-endorsement-1"}]})
        if path == "/proxy/gmail/v1/users/me/messages/msg-issued-endorsement-1":
            if request.url.params.get("format") == "metadata":
                return httpx.Response(
                    200,
                    json={"payload": {"headers": [{"name": "Subject", "value": _LIVE_SUBJECT}]}},
                )
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "uw@ironcladcasualty.com"},
                            {"name": "Subject", "value": _LIVE_SUBJECT},
                            {"name": "Date", "value": "Wed, 24 Sep 2027 13:00:00 -0500"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": _b64url(body.encode("utf-8"))},
                    }
                },
            )
        return httpx.Response(404, json={"error": f"unhandled {request.method} {path}"})

    return handler


@pytest.fixture
async def live_gmail_connected(es_ctx, es_session, monkeypatch):
    """A tenant with Gmail "connected" and CONNECTORS_MODE forced to "live"
    for this test only — ``get_settings()`` is ``@lru_cache``d, so the cache
    must be cleared on both sides of the env-var flip (same fix used by this
    session's Quote Comparison/Binder & Issuance live tests)."""
    monkeypatch.setenv("CONNECTORS_MODE", "live")
    get_settings.cache_clear()
    await upsert_connection(
        es_session, es_ctx.tenant_id, "google-mail",
        nango_connection_id="conn-live-ep", status="connected",
    )
    yield
    get_settings.cache_clear()


def _patch_gmail(monkeypatch, handler) -> None:
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured once at import time — see
# test_es_binder_issuance.py's identical fix: a test patching httpx.AsyncClient
# twice in the same test must always rebuild from the TRUE original.


async def _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch):
    """Chains real Quote Comparison (Ironclad's clean, routine-only
    scenario_01 quote) -> real Binder & Issuance creation -> a real,
    mocked-Gmail bind confirmation attach, producing a genuine SENT/CLEAN
    bind — the only state an endorsement can legitimately start from."""
    qc_item = await run_quote_comparison(
        QuoteRunRequest(scenario_ref="scenario_01", as_of="2027-07-29"), es_ctx, es_session
    )
    ironclad_quote = next(
        q for q in qc_item.payload.quotes if q.carrier_name == "Ironclad Casualty Solutions"
    )
    await select_quote(qc_item.id, ironclad_quote.quote_id, es_ctx, es_session)
    bind_item = await run_binder_issuance_from_quote(
        RunFromQuoteRequest(quote_comparison_item_id=qc_item.id), es_ctx, es_session
    )
    assert bind_item.payload.bind_order_status == "READY"

    _patch_gmail(monkeypatch, _bind_confirmation_gmail_handler)
    confirmed = await attach_live_bind_confirmation(
        bind_item.id,
        AttachLiveBindConfirmationRequest(message_id="msg-bind-confirmation-1"),
        es_ctx, es_session,
    )
    assert confirmed.payload.bind_order_status == "SENT"
    assert confirmed.payload.carrier_confirmation.reconciliation_status == "CLEAN"
    return confirmed


async def test_run_live_from_binder_requires_sent_bind(es_ctx, es_session) -> None:
    """An endorsement only applies to an already-bound policy — starting
    one from a bind that's still READY (no confirmation yet) must be
    rejected, not silently proceed on incomplete data."""
    qc_item = await run_quote_comparison(
        QuoteRunRequest(scenario_ref="scenario_01", as_of="2027-07-29"), es_ctx, es_session
    )
    ironclad_quote = next(
        q for q in qc_item.payload.quotes if q.carrier_name == "Ironclad Casualty Solutions"
    )
    await select_quote(qc_item.id, ironclad_quote.quote_id, es_ctx, es_session)
    bind_item = await run_binder_issuance_from_quote(
        RunFromQuoteRequest(quote_comparison_item_id=qc_item.id), es_ctx, es_session
    )
    assert bind_item.payload.bind_order_status == "READY"

    with pytest.raises(HTTPException) as exc_info:
        await run_endorsement_from_binder(
            RunLiveFromBinderRequest(
                binder_issuance_item_id=bind_item.id,
                change_type="additional_insured_endorsement",
                change_detail="Add Riverside Fabrication Co. as additional insured",
            ),
            es_ctx, es_session,
        )
    assert exc_info.value.status_code == 409


async def test_run_live_from_binder_uses_real_bind_terms(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    bind_item = await _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch)

    ep_item = await run_endorsement_from_binder(
        RunLiveFromBinderRequest(
            binder_issuance_item_id=bind_item.id,
            change_type="additional_insured_endorsement",
            change_detail="Add Riverside Fabrication Co. as additional insured",
        ),
        es_ctx, es_session,
    )
    payload = ep_item.payload
    assert payload.named_insured == _LIVE_NAMED_INSURED
    assert payload.carrier_name == "Ironclad Casualty Solutions"
    # bind_id is Binder & Issuance's OWN stable bind_id (payload["bind_id"]),
    # NOT this binder-issuance review item's own id — a real bug found and
    # fixed this pass: Renewal Remarketing cross-references endorsement
    # history by this exact value, which only works if both sides agree.
    assert payload.bind_id == bind_item.payload.bind_id
    assert payload.bind_id != bind_item.id
    assert payload.classification == "ROUTINE"  # additional_insured is ALWAYS_ROUTINE_TYPES
    assert payload.appetite_recheck.outcome == "NOT_APPLICABLE"  # doesn't touch appetite-types
    assert payload.requested_items == ["Riverside Fabrication Co."]


async def test_run_live_from_binder_carries_real_expiration_date(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """Gap-fill bonus fix: Binder & Issuance's real (assumed-default)
    expiration_date now flows into current_terms, unblocking proration
    logic that was already written but previously silently no-op'd for
    every real live endorsement (it only ever checked for a key that
    never existed)."""
    bind_item = await _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch)
    ep_item = await run_endorsement_from_binder(
        RunLiveFromBinderRequest(
            binder_issuance_item_id=bind_item.id,
            change_type="limit_increase",
            change_detail=(
                "Increase General Liability limits to $2,000,000/$4,000,000, "
                "effective 10/01/2027"
            ),
        ),
        es_ctx, es_session,
    )
    proration = ep_item.payload.premium_impact.proration_inputs
    assert proration is not None  # previously always None for every real live bind
    assert proration.term_total_days == 365


async def test_run_live_from_binder_plumbs_real_headcount_percent_change(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """Gap-fill: classify()'s already-computed percent_change/absolute_change
    for a material employee_count_update endorsement — previously computed
    and immediately discarded — now reaches the output payload, where
    Renewal Remarketing's RR-01 reads it as a real exposure-change signal."""
    bind_item = await _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch)
    ep_item = await run_endorsement_from_binder(
        RunLiveFromBinderRequest(
            binder_issuance_item_id=bind_item.id,
            change_type="employee_count_update",
            change_detail="Headcount growth from 50 to 81 employees",
        ),
        es_ctx, es_session,
    )
    payload = ep_item.payload
    assert payload.classification == "UNDERWRITING_REVIEW_REQUIRED"
    assert payload.requested_change.percent_change == 62.0
    assert payload.requested_change.absolute_change == 31.0


async def test_live_inbox_discovers_real_candidate_messages(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    bind_item = await _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch)
    ep_item = await run_endorsement_from_binder(
        RunLiveFromBinderRequest(
            binder_issuance_item_id=bind_item.id,
            change_type="additional_insured_endorsement",
            change_detail="Add Riverside Fabrication Co. as additional insured",
        ),
        es_ctx, es_session,
    )

    _patch_gmail(monkeypatch, _issued_endorsement_gmail_handler(items=["Riverside Fabrication Co."]))
    messages = await list_live_inbox(ep_item.id, es_ctx, es_session)
    assert {m.id for m in messages} == {"msg-issued-endorsement-1"}


async def test_attach_live_issued_endorsement_clean_fires_trigger(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    bind_item = await _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch)
    ep_item = await run_endorsement_from_binder(
        RunLiveFromBinderRequest(
            binder_issuance_item_id=bind_item.id,
            change_type="additional_insured_endorsement",
            change_detail="Add Riverside Fabrication Co. as additional insured",
        ),
        es_ctx, es_session,
    )
    before = await list_agent_communication(es_ctx, es_session)

    _patch_gmail(monkeypatch, _issued_endorsement_gmail_handler(items=["Riverside Fabrication Co."]))
    updated = await attach_live_issued_endorsement(
        ep_item.id, AttachLiveMessageRequest(message_id="msg-issued-endorsement-1"), es_ctx, es_session
    )
    payload = updated.payload
    assert payload.carrier_response.reconciliation_status == "CLEAN"
    assert payload.carrier_response.endorsement_number == "END-2027-TEST"
    assert payload.downstream_trigger_fired is True

    after = await list_agent_communication(es_ctx, es_session)
    assert len(after) == len(before) + 1


async def test_attach_live_issued_endorsement_flags_partial_discrepancy(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """Requesting two additional insureds but the carrier's issued
    endorsement only confirms one — EP-05's item-level check must flag
    this as a partial fulfillment, never a false-clean reconciliation."""
    bind_item = await _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch)
    ep_item = await run_endorsement_from_binder(
        RunLiveFromBinderRequest(
            binder_issuance_item_id=bind_item.id,
            change_type="additional_insured_endorsement",
            change_detail=(
                "Add BOTH Midstate Distribution Co. AND Harborline Logistics as scheduled "
                "additional insureds"
            ),
        ),
        es_ctx, es_session,
    )
    assert ep_item.payload.requested_items == ["Midstate Distribution Co.", "Harborline Logistics"]
    before = await list_agent_communication(es_ctx, es_session)

    _patch_gmail(monkeypatch, _issued_endorsement_gmail_handler(items=["Midstate Distribution Co."]))
    updated = await attach_live_issued_endorsement(
        ep_item.id, AttachLiveMessageRequest(message_id="msg-issued-endorsement-1"), es_ctx, es_session
    )
    payload = updated.payload
    assert payload.carrier_response.reconciliation_status == "DISCREPANCY_FLAGGED"
    missing = [d.requested_item for d in payload.carrier_response.discrepancy_detail]
    assert missing == ["Harborline Logistics"]
    assert payload.downstream_trigger_fired is False

    after = await list_agent_communication(es_ctx, es_session)
    assert len(after) == len(before)  # never fires on an unresolved discrepancy


async def test_attach_live_issued_endorsement_never_refires_trigger(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """Regression guard: re-attaching (e.g. the broker re-picks the same
    email) after it's already CLEAN must not fire a second
    ENDORSEMENT_CONFIRMED draft."""
    bind_item = await _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch)
    ep_item = await run_endorsement_from_binder(
        RunLiveFromBinderRequest(
            binder_issuance_item_id=bind_item.id,
            change_type="additional_insured_endorsement",
            change_detail="Add Riverside Fabrication Co. as additional insured",
        ),
        es_ctx, es_session,
    )
    _patch_gmail(monkeypatch, _issued_endorsement_gmail_handler(items=["Riverside Fabrication Co."]))
    await attach_live_issued_endorsement(
        ep_item.id, AttachLiveMessageRequest(message_id="msg-issued-endorsement-1"), es_ctx, es_session
    )
    after_first = await list_agent_communication(es_ctx, es_session)

    await attach_live_issued_endorsement(
        ep_item.id, AttachLiveMessageRequest(message_id="msg-issued-endorsement-1"), es_ctx, es_session
    )
    after_second = await list_agent_communication(es_ctx, es_session)
    assert len(after_second) == len(after_first)
