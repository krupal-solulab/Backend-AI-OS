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

import base64
from datetime import date

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, WorkflowInput
from core.common.enums import DecisionOutcome, ReviewAction, ReviewStatus, Role, Vertical
from core.config import get_settings
from core.integrations.repository import upsert_connection
from core.llm import build_llm_service
from core.models import Tenant
from core.review_queue import DefaultReviewQueueService
from verticals.es.workflows.agent_communication.router import list_agent_communication
from verticals.es.workflows.binder_issuance.coordination_engine import recompute_live_state
from verticals.es.workflows.binder_issuance.router import (
    AttachLiveMessageRequest,
    ClearSubjectivityRequest,
    ResolveDiscrepancyRequest,
    RunFromQuoteRequest,
    RunRequest,
    attach_live_confirmation,
    attach_live_policy,
    clear_subjectivity,
    escalate,
    list_live_inbox,
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


async def test_bind_terms_carry_assumed_default_expiration_date(es_ctx, es_session) -> None:
    """Gap-fill: no real source document (bind confirmation email or issued
    policy declarations page) ever states an expiration date — confirmed
    against every real sample in this dataset. A standard 12-month term is
    derived from the real effective_date and always flagged as an assumed
    default, so downstream consumers (Endorsement Processing's proration,
    Renewal Remarketing's FR-1/RR-07) never treat it as more certain than
    it is."""
    qc_item = await run_quote_comparison(
        QuoteRunRequest(scenario_ref="scenario_01", as_of="2027-07-29"), es_ctx, es_session
    )
    meridian_quote = next(
        q for q in qc_item.payload.quotes if q.carrier_name == "Meridian Excess & Surplus"
    )
    await select_quote(qc_item.id, meridian_quote.quote_id, es_ctx, es_session)
    item = await run_binder_issuance_from_quote(
        RunFromQuoteRequest(quote_comparison_item_id=qc_item.id), es_ctx, es_session
    )
    terms = item.payload.requested_bind_terms
    assert terms.effective_date == "2027-09-01"
    assert terms.expiration_date == "2028-08-31"  # +365 days — 2028 is a leap year
    assert terms.expiration_date_is_assumed_default is True


async def test_run_from_quote_comparison_requires_a_selection_first(es_ctx, es_session) -> None:
    qc_item = await run_quote_comparison(
        QuoteRunRequest(scenario_ref="scenario_01", as_of="2027-07-29"), es_ctx, es_session
    )
    with pytest.raises(HTTPException) as exc_info:
        await run_binder_issuance_from_quote(
            RunFromQuoteRequest(quote_comparison_item_id=qc_item.id), es_ctx, es_session
        )
    assert exc_info.value.status_code == 409


# --- Live path: attach a real carrier confirmation / issued policy --------
#
# Unlike the scenario_* tests above (static Workflow_14 fixture), these prove
# the NEW live-attach path added this session: a real bind item (already
# live via run_binder_issuance_from_quote) gets a REAL carrier confirmation
# email attached (BI-03), then a REAL issued policy (BI-05) — same mocked-
# Nango-proxy technique as tests/test_es_quote_comparison.py's live tests.

_LIVE_NAMED_INSURED = "Delta Electric Services LLC"
_LIVE_SUBJECT = f"BOUND - {_LIVE_NAMED_INSURED}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


async def _ready_ironclad_bind_item(es_ctx, es_session):
    """Ironclad's scenario_01 quote has only ROUTINE subjectivities (the
    clean fallback to Meridian's contingent offer) — selecting it produces
    a real bind item that's READY, not BLOCKED, so the live-confirmation
    flow below has something to attach to."""
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
    return bind_item


def _confirmation_gmail_handler(*, premium: str = "26,500"):
    body = (
        f"We're pleased to confirm your bind on {_LIVE_NAMED_INSURED}.\n\n"
        "Binder Number: IRO-2027-TEST\n"
        f"Premium: ${premium}\n"
        "General Liability: $1,000,000 / $2,000,000 aggregate\n"
        "Deductible: $2,500\n"
        "Effective Date: 09/01/2027\n"
        "Policy documents to follow within 30 days.\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/proxy/gmail/v1/users/me/messages" and request.method == "GET":
            return httpx.Response(200, json={"messages": [{"id": "msg-confirmation-1"}]})
        if path == "/proxy/gmail/v1/users/me/messages/msg-confirmation-1":
            if request.url.params.get("format") == "metadata":
                return httpx.Response(
                    200, json={"payload": {"headers": [{"name": "Subject", "value": _LIVE_SUBJECT}]}}
                )
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "uw@ironcladcasualty.com"},
                            {"name": "Subject", "value": _LIVE_SUBJECT},
                            {"name": "Date", "value": "Fri, 30 Jul 2027 16:00:00 -0500"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": _b64url(body.encode("utf-8"))},
                    }
                },
            )
        return httpx.Response(404, json={"error": f"unhandled {request.method} {path}"})

    return handler


_POLICY_MESSAGE_HANDLER_BODY = (
    "POLICY DECLARATIONS PAGE (extracted text)\n"
    f"Named Insured: {_LIVE_NAMED_INSURED}\n"
    "Policy Number: IRO-POL-2027-TEST\n"
    "Premium: $26,500\n"
    "General Liability Limits: $1,000,000/$2,000,000\n"
    "All Perils: $2,500\n"
    "Effective Date: 09/01/2027\n"
)


def _policy_gmail_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/proxy/gmail/v1/users/me/messages" and request.method == "GET":
        return httpx.Response(200, json={"messages": [{"id": "msg-policy-1"}]})
    if path == "/proxy/gmail/v1/users/me/messages/msg-policy-1":
        if request.url.params.get("format") == "metadata":
            return httpx.Response(
                200, json={"payload": {"headers": [{"name": "Subject", "value": _LIVE_SUBJECT}]}}
            )
        return httpx.Response(
            200,
            json={
                "payload": {
                    "mimeType": "text/plain",
                    "body": {"data": _b64url(_POLICY_MESSAGE_HANDLER_BODY.encode("utf-8"))},
                }
            },
        )
    return httpx.Response(404, json={"error": f"unhandled {request.method} {path}"})


@pytest.fixture
async def live_gmail_connected(es_ctx, es_session, monkeypatch):
    """A tenant with Gmail "connected" and CONNECTORS_MODE forced to "live"
    for this test only — ``get_settings()`` is ``@lru_cache``d, so the cache
    must be cleared on both sides of the env-var flip (see this session's
    Quote Comparison live tests for the same fix)."""
    monkeypatch.setenv("CONNECTORS_MODE", "live")
    get_settings.cache_clear()
    await upsert_connection(
        es_session, es_ctx.tenant_id, "google-mail",
        nango_connection_id="conn-live-bi", status="connected",
    )
    yield
    get_settings.cache_clear()


_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured once at import time — a test that
# patches httpx.AsyncClient twice (confirmation handler, then policy handler) must
# always rebuild from the TRUE original, not whatever monkeypatch last set it to
# (monkeypatch only restores at test teardown, not between same-test setattr calls).


def _patch_gmail(monkeypatch, handler) -> None:
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_live_inbox_discovers_real_candidate_messages(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    bind_item = await _ready_ironclad_bind_item(es_ctx, es_session)
    _patch_gmail(monkeypatch, _confirmation_gmail_handler())

    messages = await list_live_inbox(bind_item.id, es_ctx, es_session)
    assert {m.id for m in messages} == {"msg-confirmation-1"}
    assert messages[0].subject == _LIVE_SUBJECT


async def test_attach_live_confirmation_clean_fires_placement_confirmation(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    bind_item = await _ready_ironclad_bind_item(es_ctx, es_session)
    _patch_gmail(monkeypatch, _confirmation_gmail_handler())
    before = await list_agent_communication(es_ctx, es_session)

    updated = await attach_live_confirmation(
        bind_item.id, AttachLiveMessageRequest(message_id="msg-confirmation-1"), es_ctx, es_session
    )
    payload = updated.payload
    assert payload.bind_order_status == "SENT"
    assert payload.carrier_confirmation.reconciliation_status == "CLEAN"
    assert payload.carrier_confirmation.binder_number == "IRO-2027-TEST"
    assert payload.downstream_triggers_fired.placement_confirmation is True

    after = await list_agent_communication(es_ctx, es_session)
    assert len(after) == len(before) + 1


async def test_attach_live_confirmation_flags_real_discrepancy(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """Confirmation premium ($30,000) doesn't match the requested $26,500 —
    BI-03 must flag it, never silently accept the carrier's number."""
    bind_item = await _ready_ironclad_bind_item(es_ctx, es_session)
    _patch_gmail(monkeypatch, _confirmation_gmail_handler(premium="30,000"))
    before = await list_agent_communication(es_ctx, es_session)

    updated = await attach_live_confirmation(
        bind_item.id, AttachLiveMessageRequest(message_id="msg-confirmation-1"), es_ctx, es_session
    )
    payload = updated.payload
    assert payload.bind_order_status == "SENT"
    assert payload.carrier_confirmation.reconciliation_status == "DISCREPANCY_FLAGGED"
    assert any(d.field == "premium" for d in payload.carrier_confirmation.discrepancy_detail)
    assert payload.downstream_triggers_fired.placement_confirmation is False

    after = await list_agent_communication(es_ctx, es_session)
    assert len(after) == len(before)  # never fires on an unresolved discrepancy


async def test_attach_live_confirmation_rejected_when_blocked(es_ctx, es_session) -> None:
    """Meridian's quote carries a still-open material subjectivity —
    BLOCKED — attaching a confirmation must be rejected, not silently
    proceed past an unresolved pre-bind blocker (BI-02)."""
    qc_item = await run_quote_comparison(
        QuoteRunRequest(scenario_ref="scenario_01", as_of="2027-07-29"), es_ctx, es_session
    )
    meridian_quote = next(
        q for q in qc_item.payload.quotes if q.carrier_name == "Meridian Excess & Surplus"
    )
    await select_quote(qc_item.id, meridian_quote.quote_id, es_ctx, es_session)
    bind_item = await run_binder_issuance_from_quote(
        RunFromQuoteRequest(quote_comparison_item_id=qc_item.id), es_ctx, es_session
    )
    assert bind_item.payload.bind_order_status == "BLOCKED"

    with pytest.raises(HTTPException) as exc_info:
        await attach_live_confirmation(
            bind_item.id, AttachLiveMessageRequest(message_id="msg-confirmation-1"), es_ctx, es_session
        )
    assert exc_info.value.status_code == 409


async def test_clear_subjectivity_unblocks_a_real_blocked_bind(es_ctx, es_session) -> None:
    """Same real BLOCKED bind as test_attach_live_confirmation_rejected_when_blocked
    above — proves the previously-permanent dead end is now real and
    fixable: clearing the open material subjectivity flips BLOCKED -> READY,
    and a confirmation can then be attached without the 409."""
    qc_item = await run_quote_comparison(
        QuoteRunRequest(scenario_ref="scenario_01", as_of="2027-07-29"), es_ctx, es_session
    )
    meridian_quote = next(
        q for q in qc_item.payload.quotes if q.carrier_name == "Meridian Excess & Surplus"
    )
    await select_quote(qc_item.id, meridian_quote.quote_id, es_ctx, es_session)
    bind_item = await run_binder_issuance_from_quote(
        RunFromQuoteRequest(quote_comparison_item_id=qc_item.id), es_ctx, es_session
    )
    assert bind_item.payload.bind_order_status == "BLOCKED"
    open_material = next(
        s for s in bind_item.payload.pre_bind_subjectivities
        if s.materiality == "material" and s.status == "open"
    )

    updated = await clear_subjectivity(
        bind_item.id, ClearSubjectivityRequest(description=open_material.description),
        es_ctx, es_session,
    )

    assert updated.payload.bind_order_status == "READY"
    cleared = next(
        s for s in updated.payload.pre_bind_subjectivities
        if s.description == open_material.description
    )
    assert cleared.status == "cleared"

    # And attach-confirmation, previously rejected with 409 (BLOCKED), no longer is.
    with pytest.raises(HTTPException) as exc_info:
        await attach_live_confirmation(
            bind_item.id, AttachLiveMessageRequest(message_id="msg-confirmation-1"), es_ctx, es_session
        )
    # Now fails for a DIFFERENT reason (no live Gmail connector mocked in this
    # test) rather than the 409 "still BLOCKED" this test exists to disprove.
    assert exc_info.value.status_code != 409


async def test_clear_subjectivity_404s_on_unknown_description(es_ctx, es_session) -> None:
    bind_item = await _ready_ironclad_bind_item(es_ctx, es_session)
    with pytest.raises(HTTPException) as exc_info:
        await clear_subjectivity(
            bind_item.id, ClearSubjectivityRequest(description="no such subjectivity"),
            es_ctx, es_session,
        )
    assert exc_info.value.status_code == 404


async def test_attach_live_policy_requires_confirmation_first(es_ctx, es_session) -> None:
    bind_item = await _ready_ironclad_bind_item(es_ctx, es_session)
    with pytest.raises(HTTPException) as exc_info:
        await attach_live_policy(
            bind_item.id, AttachLiveMessageRequest(message_id="msg-policy-1"), es_ctx, es_session
        )
    assert exc_info.value.status_code == 409


async def test_attach_live_policy_clean_fires_only_the_new_trigger(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """The important regression guard: attaching the issued policy after the
    confirmation is already clean must fire Policy Documents Delivered
    exactly once, and must NOT re-fire Placement Confirmation a second time
    (fire_binder_issuance_result isn't idempotent on its own — the router
    must diff old vs. new downstream_triggers_fired)."""
    bind_item = await _ready_ironclad_bind_item(es_ctx, es_session)
    _patch_gmail(monkeypatch, _confirmation_gmail_handler())
    await attach_live_confirmation(
        bind_item.id, AttachLiveMessageRequest(message_id="msg-confirmation-1"), es_ctx, es_session
    )
    after_confirmation = await list_agent_communication(es_ctx, es_session)

    _patch_gmail(monkeypatch, _policy_gmail_handler)
    updated = await attach_live_policy(
        bind_item.id, AttachLiveMessageRequest(message_id="msg-policy-1"), es_ctx, es_session
    )
    payload = updated.payload
    assert payload.issued_policy_reconciliation.status == "CLEAN"
    assert payload.policy_issuance.documents_received is True
    assert payload.downstream_triggers_fired.policy_documents_delivered is True

    after_policy = await list_agent_communication(es_ctx, es_session)
    assert len(after_policy) == len(after_confirmation) + 1  # exactly one NEW draft, not two


async def test_attach_live_policy_flags_real_discrepancy(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """Issued policy's premium doesn't match the confirmed $26,500 —
    Scenario 06's exact single-field-mismatch failure mode, proven here on
    the live path instead of the fixture."""
    bind_item = await _ready_ironclad_bind_item(es_ctx, es_session)
    _patch_gmail(monkeypatch, _confirmation_gmail_handler())
    await attach_live_confirmation(
        bind_item.id, AttachLiveMessageRequest(message_id="msg-confirmation-1"), es_ctx, es_session
    )

    def mismatched_policy_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/proxy/gmail/v1/users/me/messages" and request.method == "GET":
            return httpx.Response(200, json={"messages": [{"id": "msg-policy-2"}]})
        if path == "/proxy/gmail/v1/users/me/messages/msg-policy-2":
            body = _POLICY_MESSAGE_HANDLER_BODY.replace("Premium: $26,500", "Premium: $27,000")
            return httpx.Response(
                200,
                json={"payload": {"mimeType": "text/plain", "body": {"data": _b64url(body.encode())}}},
            )
        return httpx.Response(404, json={"error": "unexpected"})

    _patch_gmail(monkeypatch, mismatched_policy_handler)
    updated = await attach_live_policy(
        bind_item.id, AttachLiveMessageRequest(message_id="msg-policy-2"), es_ctx, es_session
    )
    payload = updated.payload
    assert payload.issued_policy_reconciliation.status == "POLICY_DISCREPANCY_FLAGGED"
    assert any(d.field == "premium" for d in payload.issued_policy_reconciliation.discrepancy_detail)
    assert payload.downstream_triggers_fired.policy_documents_delivered is False
