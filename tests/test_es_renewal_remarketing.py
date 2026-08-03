"""E&S Renewal Remarketing eval test — proves the pipeline + router against
the REAL Workflow_16 dataset (originally
``Data sets/Workflow 7/renewal_remarketing_dataset``, copied to
``TEST_DATA_ROOT/Workflow_16/test_dataset`` per DATA_AND_FIXTURES.md).

Pytest-discovered here (not under src/verticals/es/...) — see
verticals/es/workflows/renewal_remarketing/eval_test.py for why.
"""

from __future__ import annotations

import base64
from datetime import date, timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, WorkflowInput
from core.common.enums import ReviewStatus, Role, Vertical
from core.config import get_settings
from core.integrations.repository import upsert_connection
from core.llm import build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import Tenant
from core.review_queue import DefaultReviewQueueService
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
    RunLiveFromBinderRequest,
    run_endorsement_from_binder,
)
from verticals.es.workflows.market_matching.router import list_market_matching
from verticals.es.workflows.quote_comparison.router import (
    RunLiveRequest as QuoteRunLiveRequest,
)
from verticals.es.workflows.quote_comparison.router import (
    run_quote_comparison,
    run_quote_comparison_live,
    select_quote,
)
from verticals.es.workflows.quote_comparison.router import (
    RunRequest as QuoteRunRequest,
)
from verticals.es.workflows.renewal_remarketing.router import (
    InitiateRemarketRequest,
    RunLiveComparisonRequest,
    RunLiveRequest,
    RunRequest,
    accept_incumbent,
    escalate,
    initiate_remarket,
    list_live_alternative_quotes,
    list_live_binds,
    run_renewal_remarketing,
    run_renewal_remarketing_live,
    run_renewal_remarketing_live_comparison,
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


async def test_run_live_reflects_real_already_endorsed_exposure(es_ctx, es_session) -> None:
    """FR-2 (Phase 3 connectivity): a real bind with a real, material
    (UNDERWRITING_REVIEW_REQUIRED) endorsement on file must be flagged as
    already-endorsed via a genuine cross-reference — never a fabricated
    exposure percentage, and the trigger honestly stays NO_REMARKET since
    no real exposure-% / loss-history / incumbent-offer signal exists for
    this bind (per the approved narrow scope)."""
    bind_pkg = OutputPackageRow(
        tenant_id=es_ctx.tenant_id,
        submission_id="SUB-LIVE-RR-1",
        workflow="binder_issuance",
        payload={
            "bind_id": "BIND-LIVE-1",
            "submission_id": "SUB-LIVE-RR-1",
            "named_insured": "Live Test Insured",
            "carrier_id": "CAR-01",
            "carrier_name": "Meridian Excess & Surplus",
            "requested_bind_terms": {},
        },
    )
    endorsement_pkg = OutputPackageRow(
        tenant_id=es_ctx.tenant_id,
        submission_id="BIND-LIVE-1",  # endorsement's OutputPackage.submission_id IS the bind_id
        workflow="endorsement",
        payload={
            "endorsement_request_id": "ep-1",
            "bind_id": "BIND-LIVE-1",
            "classification": "UNDERWRITING_REVIEW_REQUIRED",
            "requested_change": {"type": "add_location", "detail": "Added a new location, +$5M TIV"},
            "drafted_request": {"body": "..."},
        },
    )
    es_session.add_all([bind_pkg, endorsement_pkg])
    await es_session.commit()

    binds = await list_live_binds(es_ctx, es_session)
    assert any(b.bind_id == "BIND-LIVE-1" for b in binds)

    item = await run_renewal_remarketing_live(
        RunLiveRequest(bind_id="BIND-LIVE-1"), es_ctx, es_session
    )
    payload = item.payload
    assert payload.named_insured == "Live Test Insured"
    assert payload.incumbent_carrier_name == "Meridian Excess & Surplus"
    assert payload.exposure_change.pct_change == 0.0  # never a guessed number
    assert payload.exposure_change.already_endorsed is True
    assert "already endorsed" in (payload.exposure_change.note or "").lower()
    assert payload.trigger_decision.level == "NO_REMARKET"


async def test_run_live_no_endorsement_never_fabricates_already_endorsed(
    es_ctx, es_session
) -> None:
    bind_pkg = OutputPackageRow(
        tenant_id=es_ctx.tenant_id,
        submission_id="SUB-LIVE-RR-2",
        workflow="binder_issuance",
        payload={
            "bind_id": "BIND-LIVE-2",
            "submission_id": "SUB-LIVE-RR-2",
            "named_insured": "Another Live Insured",
            "carrier_id": "CAR-03",
            "carrier_name": "Ironclad Casualty Solutions",
            "requested_bind_terms": {},
        },
    )
    es_session.add(bind_pkg)
    await es_session.commit()

    item = await run_renewal_remarketing_live(
        RunLiveRequest(bind_id="BIND-LIVE-2"), es_ctx, es_session
    )
    payload = item.payload
    assert payload.exposure_change.already_endorsed is False
    assert payload.exposure_change.pct_change == 0.0
    assert payload.trigger_decision.level == "NO_REMARKET"


# --- Live path: bind_id stability fix + RR-05/RR-06 live wiring -----------
#
# Same mocked-Nango-proxy technique as this session's other live tests.

_LIVE_NAMED_INSURED = "Delta Electric Services LLC"
_ALT_NAMED_INSURED = "Summit Roofing Group LLC"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _plain_message_handler(message_id: str, *, from_addr: str, subject: str, body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/proxy/gmail/v1/users/me/messages" and request.method == "GET":
            return httpx.Response(200, json={"messages": [{"id": message_id}]})
        if path == f"/proxy/gmail/v1/users/me/messages/{message_id}":
            if request.url.params.get("format") == "metadata":
                return httpx.Response(
                    200, json={"payload": {"headers": [{"name": "Subject", "value": subject}]}}
                )
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "headers": [
                            {"name": "From", "value": from_addr},
                            {"name": "Subject", "value": subject},
                            {"name": "Date", "value": "Fri, 30 Jul 2027 16:00:00 -0500"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": _b64url(body.encode("utf-8"))},
                    }
                },
            )
        return httpx.Response(404, json={"error": f"unhandled {request.method} {path}"})

    return handler


def _empty_inbox_handler(request: httpx.Request) -> httpx.Response:
    """No candidate messages at all — the honest "not received" default for
    the real trigger-time incumbent-offer check (RR-03/RR-07)."""
    if request.url.path == "/proxy/gmail/v1/users/me/messages" and request.method == "GET":
        return httpx.Response(200, json={"messages": []})
    return httpx.Response(404, json={"error": f"unhandled {request.method} {request.url.path}"})


_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured once — see test_es_binder_issuance.py's
# identical fix: a test patching httpx.AsyncClient more than once must always rebuild
# from the TRUE original, never whatever monkeypatch last set it to.


def _patch_gmail(monkeypatch, handler) -> None:
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture
async def live_gmail_connected(es_ctx, es_session, monkeypatch):
    monkeypatch.setenv("CONNECTORS_MODE", "live")
    get_settings.cache_clear()
    await upsert_connection(
        es_session, es_ctx.tenant_id, "google-mail",
        nango_connection_id="conn-live-rr", status="connected",
    )
    yield
    get_settings.cache_clear()


async def _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch):
    """Same real Quote Comparison (scenario_01, Ironclad's clean routine-
    only quote) -> real Binder & Issuance -> real, mocked-Gmail confirmation
    chain used by this session's Binder & Issuance/Endorsement live tests."""
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

    _patch_gmail(
        monkeypatch,
        _plain_message_handler(
            "msg-bind-confirmation-1",
            from_addr="uw@ironcladcasualty.com", subject="BOUND",
            body=(
                "Binder Number: IRO-2027-TEST\n"
                "Premium: $26,500\n"
                "General Liability: $1,000,000 / $2,000,000 aggregate\n"
                "Deductible: $2,500\n"
                "Effective Date: 09/01/2027\n"
            ),
        ),
    )
    confirmed = await attach_live_bind_confirmation(
        bind_item.id,
        AttachLiveBindConfirmationRequest(message_id="msg-bind-confirmation-1"),
        es_ctx, es_session,
    )
    assert confirmed.payload.bind_order_status == "SENT"
    return confirmed


async def test_bind_id_stable_across_live_reruns(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """The correctness fix this session found: attaching a real confirmation
    (a normal live re-run of the SAME bind) must never change the bind_id
    Renewal Remarketing keys a real account by — otherwise a second renewal
    cycle for the same account would silently show up as a DIFFERENT bind,
    breaking RR-08's whole premise."""
    confirmed = await _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch)
    bind_id_after_confirmation = confirmed.payload.bind_id

    binds = await list_live_binds(es_ctx, es_session)
    assert any(b.bind_id == bind_id_after_confirmation for b in binds)

    _patch_gmail(
        monkeypatch,
        _plain_message_handler(
            "msg-issued-policy-1",
            from_addr="uw@ironcladcasualty.com", subject="Policy",
            body=(
                "POLICY DECLARATIONS PAGE (extracted text)\n"
                f"Named Insured: {_LIVE_NAMED_INSURED}\n"
                "Policy Number: IRO-POL-TEST\n"
                "Premium: $26,500\n"
                "General Liability Limits: $1,000,000/$2,000,000\n"
                "All Perils: $2,500\n"
                "Effective Date: 09/01/2027\n"
            ),
        ),
    )
    from verticals.es.workflows.binder_issuance.router import (
        AttachLiveMessageRequest as AttachLivePolicyRequest,
    )
    from verticals.es.workflows.binder_issuance.router import attach_live_policy

    updated = await attach_live_policy(
        confirmed.id, AttachLivePolicyRequest(message_id="msg-issued-policy-1"), es_ctx, es_session
    )
    assert updated.payload.bind_id == bind_id_after_confirmation

    binds_after = await list_live_binds(es_ctx, es_session)
    assert any(b.bind_id == bind_id_after_confirmation for b in binds_after)


async def test_initiate_remarket_with_real_message_uses_live_market_matching(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """RR-05, fixed: passing a real message_id must re-invoke Market
    Matching against that REAL message — not the Workflow_10 fixture
    fallback (proven by asserting the created market_matching item's
    submission_id is the real message id, never a fixture ref like
    "submission_02")."""
    item = await run_renewal_remarketing(RunRequest(scenario_ref="scenario_02"), es_ctx, es_session)

    _patch_gmail(
        monkeypatch,
        _plain_message_handler(
            "msg-remarket-submission-1",
            from_addr="broker@example.com", subject="New Submission - Summit Roofing",
            body="Please quote this account.",
        ),
    )
    result = await initiate_remarket(
        item.id, es_ctx, es_session, InitiateRemarketRequest(message_id="msg-remarket-submission-1"),
    )
    assert result.payload.remarket_execution.initiated is True
    mm_item_id = result.payload.remarket_execution.market_matching_output_id
    assert mm_item_id is not None

    mm_items = await list_market_matching(es_ctx, es_session)
    mm_item = next(i for i in mm_items if i.id == mm_item_id)
    assert mm_item.submission_id == "msg-remarket-submission-1"


async def test_run_live_comparison_requires_initiated_remarket(es_ctx, es_session) -> None:
    item = await run_renewal_remarketing(RunRequest(scenario_ref="scenario_02"), es_ctx, es_session)
    with pytest.raises(HTTPException) as exc_info:
        await run_renewal_remarketing_live_comparison(
            item.id, RunLiveComparisonRequest(message_id="x", quote_comparison_item_id="y"),
            es_ctx, es_session,
        )
    assert exc_info.value.status_code == 409


async def _initiated_scenario_02_item(es_ctx, es_session, monkeypatch):
    """Initiates via a real (mocked) live message rather than the Workflow_10
    fixture fallback — these tests already run with CONNECTORS_MODE=live
    (via live_gmail_connected, needed later for the incumbent-offer/
    alternative-quote steps), and the fixture fallback path always tries a
    real Nango call under live mode, which would 400 against a fixture ref
    like "submission_05"."""
    item = await run_renewal_remarketing(RunRequest(scenario_ref="scenario_02"), es_ctx, es_session)
    _patch_gmail(
        monkeypatch,
        _plain_message_handler(
            "msg-initiate-throwaway",
            from_addr="broker@example.com", subject="New Submission - Summit Roofing",
            body="Please quote this account.",
        ),
    )
    return await initiate_remarket(
        item.id, es_ctx, es_session, InitiateRemarketRequest(message_id="msg-initiate-throwaway"),
    )


async def _real_selected_alternative_quote(es_ctx, es_session, monkeypatch, *, premium: str):
    """A real, live-ingested, already-selected Quote Comparison quote for
    the alternative side — subject line drives named_insured, exactly like
    every other Quote Comparison live test this session."""
    _patch_gmail(
        monkeypatch,
        _plain_message_handler(
            "msg-alt-quote-1",
            from_addr="uw@palmettospecialty.com",
            subject=f"RE: {_ALT_NAMED_INSURED} - Quote Terms",
            body=(
                f"Premium: ${premium}\n"
                "General Liability: $1,000,000 / $2,000,000\n"
                "Deductible: $15,000\n"
                "Effective Date: 09/01/2028\n"
            ),
        ),
    )
    qc_item = await run_quote_comparison_live(
        QuoteRunLiveRequest(submission_id="rr-alt-submission-1", message_id="msg-alt-quote-1"),
        es_ctx, es_session,
    )
    quote_id = qc_item.payload.quotes[0].quote_id
    await select_quote(qc_item.id, quote_id, es_ctx, es_session)
    return qc_item.id


async def test_run_live_comparison_clean(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    rr_item = await _initiated_scenario_02_item(es_ctx, es_session, monkeypatch)
    qc_item_id = await _real_selected_alternative_quote(es_ctx, es_session, monkeypatch, premium="171,000")

    alternatives = await list_live_alternative_quotes(rr_item.id, es_ctx, es_session)
    assert any(a.quote_comparison_item_id == qc_item_id for a in alternatives)

    _patch_gmail(
        monkeypatch,
        _plain_message_handler(
            "msg-incumbent-offer-1",
            from_addr="uw@ironcladcasualty.com",
            subject=f"RE: {_ALT_NAMED_INSURED} - Renewal Terms",
            body=(
                "Premium: $187,000\n"
                "Limits: General Liability $1,000,000/$2,000,000\n"
                "Deductible: $15,000\n"
                "Effective Date: 09/01/2028\n"
            ),
        ),
    )
    comparison_item = await run_renewal_remarketing_live_comparison(
        rr_item.id, RunLiveComparisonRequest(
            message_id="msg-incumbent-offer-1", quote_comparison_item_id=qc_item_id,
        ),
        es_ctx, es_session,
    )
    payload = comparison_item.payload
    assert payload.is_comparison_stage is True
    comparison = payload.remarket_execution.comparison_output
    assert comparison.incumbent.premium == 187000.0
    assert comparison.alternative.premium == 171000.0
    assert comparison.alternative.carrier_name == "Palmetto Specialty Underwriters"
    assert comparison.directly_comparable is True  # same limits, same deductible


async def test_run_live_comparison_flags_material_difference(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    rr_item = await _initiated_scenario_02_item(es_ctx, es_session, monkeypatch)
    qc_item_id = await _real_selected_alternative_quote(es_ctx, es_session, monkeypatch, premium="171,000")

    _patch_gmail(
        monkeypatch,
        _plain_message_handler(
            "msg-incumbent-offer-2",
            from_addr="uw@ironcladcasualty.com",
            subject=f"RE: {_ALT_NAMED_INSURED} - Renewal Terms",
            body=(
                "Premium: $187,000\n"
                "Limits: General Liability $1,000,000/$2,000,000\n"
                "Deductible: $5,000\n"  # differs from the alternative's $15,000
                "Effective Date: 09/01/2028\n"
            ),
        ),
    )
    comparison_item = await run_renewal_remarketing_live_comparison(
        rr_item.id, RunLiveComparisonRequest(
            message_id="msg-incumbent-offer-2", quote_comparison_item_id=qc_item_id,
        ),
        es_ctx, es_session,
    )
    comparison = comparison_item.payload.remarket_execution.comparison_output
    assert comparison.directly_comparable is False
    assert "deductible" in comparison.material_differences


# --- Gap-fill: RR-01/RR-07/RR-08 now genuinely reachable from real data --


async def test_run_live_material_headcount_endorsement_reaches_light_remarket_check(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """Gap-fill (Fix 2/2b): Endorsement Processing's already-computed
    percent_change for a material employee_count_update endorsement —
    previously silently discarded — now genuinely drives RR-01 instead of a
    hardcoded 0.0, proving LIGHT_REMARKET_CHECK is reachable from 100% real
    data (exposure.material and already_endorsed, both real, no loss/
    incumbent signal needed)."""
    bind_item = await _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch)
    await run_endorsement_from_binder(
        RunLiveFromBinderRequest(
            binder_issuance_item_id=bind_item.id,
            change_type="employee_count_update",
            change_detail="Headcount growth from 50 to 81 employees",
        ),
        es_ctx, es_session,
    )

    _patch_gmail(monkeypatch, _empty_inbox_handler)
    item = await run_renewal_remarketing_live(
        RunLiveRequest(bind_id=bind_item.payload.bind_id), es_ctx, es_session
    )
    payload = item.payload
    assert payload.exposure_change.pct_change == 62.0
    assert payload.exposure_change.material is True
    assert payload.exposure_change.already_endorsed is True
    assert payload.trigger_decision.level == "LIGHT_REMARKET_CHECK"


async def test_run_live_urgent_remarket_from_real_non_response(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """Gap-fill (Fix 1/3): a real (assumed-default) expiration_date close to
    today, with no incumbent-offer email anywhere in the connected inbox,
    now genuinely fires URGENT_REMARKET — the PRD's own "zero missed urgent
    triggers" hard gate, reachable from real data for the first time."""
    near_expiration_effective = (date.today() - timedelta(days=340)).strftime("%m/%d/%Y")
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
    _patch_gmail(
        monkeypatch,
        _plain_message_handler(
            "msg-bind-confirmation-urgent",
            from_addr="uw@ironcladcasualty.com", subject="BOUND",
            body=(
                "Binder Number: IRO-2027-URGENT\n"
                "Premium: $26,500\n"
                "General Liability: $1,000,000 / $2,000,000 aggregate\n"
                "Deductible: $2,500\n"
                f"Effective Date: {near_expiration_effective}\n"
            ),
        ),
    )
    confirmed = await attach_live_bind_confirmation(
        bind_item.id,
        AttachLiveBindConfirmationRequest(message_id="msg-bind-confirmation-urgent"),
        es_ctx, es_session,
    )
    assert confirmed.payload.bind_order_status == "SENT"

    _patch_gmail(monkeypatch, _empty_inbox_handler)
    item = await run_renewal_remarketing_live(
        RunLiveRequest(bind_id=confirmed.payload.bind_id), es_ctx, es_session
    )
    payload = item.payload
    assert payload.incumbent_status.renewal_terms_received is False
    assert payload.incumbent_status.non_response_flag is True
    assert payload.trigger_decision.level == "URGENT_REMARKET"


async def test_run_live_reads_real_remarketing_history_across_cycles(
    es_ctx, es_session, live_gmail_connected, monkeypatch
) -> None:
    """Gap-fill (Fix 4): a second real renewal cycle for the SAME named
    insured (a different real bind — this dataset's equivalent of a re-bind
    at renewal) must see the first cycle's real trigger level as its
    remarketing history — never a hand-authored or simulated history."""
    bind_a = await _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch)
    _patch_gmail(monkeypatch, _empty_inbox_handler)
    review_a = await run_renewal_remarketing_live(
        RunLiveRequest(bind_id=bind_a.payload.bind_id), es_ctx, es_session
    )
    assert review_a.payload.trigger_decision.level == "NO_REMARKET"
    assert review_a.payload.remarketing_history_detail is None  # first-ever cycle (FR-11)

    bind_b = await _sent_ironclad_bind_item(es_ctx, es_session, monkeypatch)
    _patch_gmail(monkeypatch, _empty_inbox_handler)
    review_b = await run_renewal_remarketing_live(
        RunLiveRequest(bind_id=bind_b.payload.bind_id), es_ctx, es_session
    )
    assert review_b.payload.remarketing_history_detail is not None
    assert "NO_REMARKET" in review_b.payload.remarketing_history_detail
