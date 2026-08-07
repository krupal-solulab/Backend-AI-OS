"""E&S Quote Comparison eval test — proves the pipeline + router against the
REAL Workflow_13 dataset (originally
``Data sets/Workflow 4/quote_comparison_dataset``, copied to
``TEST_DATA_ROOT/Workflow_13/test_dataset`` per DATA_AND_FIXTURES.md; see
that file's Workflow_13 layout note for the raw-email fixture shape and the
"as of" reference-date convention each test below supplies explicitly).

Pytest-discovered here (not under src/verticals/es/...) — see
verticals/es/workflows/quote_comparison/eval_test.py for why.
"""

from __future__ import annotations

import base64
from datetime import date

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, RawDocument, WorkflowInput
from core.common.enums import DecisionOutcome, DocumentKind, ReviewAction, ReviewStatus, Role, Vertical
from core.config import get_settings
from core.documents import LocalDocumentStore
from core.integrations.repository import upsert_connection
from core.llm import build_llm_service
from core.models import Tenant
from core.review_queue import DefaultReviewQueueService
from verticals.es.workflows.agent_communication.router import list_agent_communication
from verticals.es.workflows.quote_comparison.comparison_engine import (
    Quote,
    recommend,
    recompute_urgency_from_payload,
)
from verticals.es.workflows.quote_comparison.quote_parser import ParsedResponse, Subjectivity
from verticals.es.workflows.quote_comparison.router import (
    RunLiveRequest,
    RunRequest,
    list_live_inbox,
    run_quote_comparison,
    run_quote_comparison_live,
    select_quote,
)
from verticals.es.workflows.quote_comparison.service import QuoteComparisonPipeline

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_13 fixtures unavailable",
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


def _pipeline() -> QuoteComparisonPipeline:
    return QuoteComparisonPipeline(llm=build_llm_service())


async def _run(ctx: Ctx, scenario_ref: str, as_of: str):
    pipeline = _pipeline()
    return await pipeline.run(ctx, WorkflowInput(source_ref=scenario_ref, params={"as_of": as_of}))


async def test_scenario_01_single_recommendation_with_contingency(es_ctx) -> None:
    """Meridian is cheaper but contingent on outstanding loss-run years;
    Ironclad is the clean fallback — must stay SINGLE_RECOMMENDATION (both
    quotes directly comparable), not a flat "cheapest wins" statement."""
    output = await _run(es_ctx, "scenario_01", as_of="2027-07-29")
    assert output.decision.outcome is DecisionOutcome.PROCEED
    payload = output.payload
    assert payload["comparability_assessment"]["directly_comparable"] is True
    assert payload["output_mode"] == "SINGLE_RECOMMENDATION"
    primary_id = payload["recommendation"]["primary_quote_id"]
    primary = next(q for q in payload["quotes"] if q["quote_id"] == primary_id)
    assert primary["carrier_name"] == "Meridian Excess & Surplus"
    assert "contingent" in payload["recommendation"]["reasoning"]["summary"].lower() or (
        "fallback" in payload["recommendation"]["reasoning"]["summary"].lower()
    )
    material = [s for s in primary["subjectivities"] if s["materiality"] == "material"]
    assert any("loss history" in s["description"].lower() for s in material)


async def test_scenario_02_multi_option_deductible_mismatch(es_ctx) -> None:
    """Harbor Specialty is ~9% cheaper but carries materially higher
    deductibles — must present as a trade-off, never a flat premium win."""
    output = await _run(es_ctx, "scenario_02", as_of="2027-07-28")
    payload = output.payload
    assert payload["comparability_assessment"]["directly_comparable"] is False
    material_diffs = payload["comparability_assessment"]["material_differences"]
    assert any("deductible" in d for d in material_diffs)
    assert payload["output_mode"] == "MULTI_OPTION"
    assert payload["recommendation"]["primary_quote_id"] is None

    harbor = next(q for q in payload["quotes"] if q["carrier_name"] == "Harbor Specialty Property")
    coastal = next(q for q in payload["quotes"] if q["carrier_name"] == "Coastal Mutual Specialty")
    assert harbor["deductibles"] == {"all_perils": "$25,000", "wind_hail": "$100,000"}
    assert coastal["deductibles"] == {"all_perils": "$10,000", "wind_hail": "$50,000"}


async def test_scenario_03_single_quote_urgent_and_consistent_declination(es_ctx) -> None:
    """Only Ironclad is bindable, with ~1 day left on its validity window —
    URGENT. Palmetto's declination must log as a CONSISTENT signal against
    its own known $150,000 severity ceiling (the $180,000 reserve exceeds it)."""
    output = await _run(es_ctx, "scenario_03", as_of="2027-07-28")
    payload = output.payload
    assert payload["output_mode"] == "SINGLE_QUOTE_URGENT"
    assert any(f["flag_type"] == "validity_window" for f in payload["urgency_flags"])

    declination = next(q for q in payload["quotes"] if q["response_type"] == "DECLINATION")
    assert declination["carrier_name"] == "Palmetto Specialty Underwriters"
    assert declination["declination_appetite_consistency"] == "consistent"
    assert declination["declination_reason"] and "180,000" in declination["declination_reason"]


async def test_scenario_04_single_quote_urgent_material_subjectivity(es_ctx) -> None:
    """The inspection-within-10-days subjectivity is what makes this urgent —
    NOT the validity window alone — while SOV-confirmed/no-new-claims stay
    routine. This is the core routine-vs-material distinction this scenario
    is built to test."""
    output = await _run(es_ctx, "scenario_04", as_of="2027-07-29")
    payload = output.payload
    assert payload["output_mode"] == "SINGLE_QUOTE_URGENT"
    quote = payload["quotes"][0]
    by_materiality = {"routine": 0, "material": 0}
    for s in quote["subjectivities"]:
        by_materiality[s["materiality"]] += 1
    assert by_materiality == {"routine": 2, "material": 1}
    material = next(s for s in quote["subjectivities"] if s["materiality"] == "material")
    assert "10 days" in material["description"]
    assert any(f["flag_type"] == "material_subjectivity" for f in payload["urgency_flags"])


async def test_scenario_05_multi_option_endorsement_mismatch(es_ctx) -> None:
    """Ironclad is cheaper but only offers scheduled (not blanket)
    additional-insured/waiver endorsements — a real trade-off for a
    facilities-management account, not a dominant option."""
    output = await _run(es_ctx, "scenario_05", as_of="2027-08-03")
    payload = output.payload
    assert payload["output_mode"] == "MULTI_OPTION"
    material_diffs = payload["comparability_assessment"]["material_differences"]
    assert any("endorsement" in d for d in material_diffs)
    ironclad = next(
        q for q in payload["quotes"] if q["carrier_name"] == "Ironclad Casualty Solutions"
    )
    ai_basis = next(
        e["basis"] for e in ironclad["key_endorsements"] if e["type"] == "Additional Insured"
    )
    assert ai_basis == "scheduled_only"


async def test_scenario_06_single_quote_urgent_dependency_flag(es_ctx) -> None:
    """Per the scenario's own system_check_context.json: 3 days remain and
    no broker action has been logged. The "primary carrier binding
    confirmed" subjectivity must surface as an UNRESOLVED DEPENDENCY (FR-16),
    distinct from a generic material-subjectivity flag."""
    output = await _run(es_ctx, "scenario_06", as_of="2027-07-30")
    payload = output.payload
    assert payload["output_mode"] == "SINGLE_QUOTE_URGENT"
    flag_types = {f["flag_type"] for f in payload["urgency_flags"]}
    assert "validity_window" in flag_types
    assert "dependency_unresolved" in flag_types
    dep_flag = next(
        f for f in payload["urgency_flags"] if f["flag_type"] == "dependency_unresolved"
    )
    assert "binding confirmed" in dep_flag["detail"].lower()


async def test_read_time_urgency_recompute_reflects_current_date() -> None:
    """QC-07/FR-14: a quote that looked routine at ingestion must show
    urgent once enough time has passed — computed fresh at read time, no
    scheduled job (per the approved plan)."""
    from datetime import date

    payload = {
        "output_mode": "SINGLE_QUOTE_ROUTINE",
        "urgency_flags": [],
        "quotes": [{
            "quote_id": "q1", "response_type": "QUOTE",
            "quote_valid_through": "08/10/2027", "subjectivities": [],
        }],
    }
    still_routine = recompute_urgency_from_payload(payload, date(2027, 7, 20))
    assert still_routine["output_mode"] == "SINGLE_QUOTE_ROUTINE"

    now_urgent = recompute_urgency_from_payload(payload, date(2027, 8, 7))
    assert now_urgent["output_mode"] == "SINGLE_QUOTE_URGENT"
    assert any(f["flag_type"] == "validity_window" for f in now_urgent["urgency_flags"])


async def test_fr20_select_fires_agent_communication_quote_terms_summary(
    es_ctx, es_session
) -> None:
    """FR-20: the BROKER'S SELECTION (not /run by itself) is what feeds
    Agent Communication's QUOTE_TERMS_SUMMARY trigger — verified end to end
    through the review queue."""
    body = RunRequest(scenario_ref="scenario_01", as_of="2027-07-29")
    item = await run_quote_comparison(body, es_ctx, es_session)
    before = await list_agent_communication(es_ctx, es_session)

    primary_id = item.payload.recommendation.primary_quote_id
    selected = await select_quote(item.id, primary_id, es_ctx, es_session)
    assert selected.payload.selected_quote_id == primary_id
    assert selected.payload.status == "PRESENTED"

    after = await list_agent_communication(es_ctx, es_session)
    assert len(after) == len(before) + 1


async def test_multi_option_does_not_autofire_before_broker_selects(es_ctx, es_session) -> None:
    """MULTI_OPTION must never auto-generate a retail-agent draft before the
    broker has actually chosen between the options (PRD FR-23's ordering)."""
    body = RunRequest(scenario_ref="scenario_02", as_of="2027-07-28")
    item = await run_quote_comparison(body, es_ctx, es_session)
    after_run = await list_agent_communication(es_ctx, es_session)
    assert len(after_run) == 0

    one_quote_id = item.payload.quotes[0].quote_id
    await select_quote(item.id, one_quote_id, es_ctx, es_session)
    after_select = await list_agent_communication(es_ctx, es_session)
    assert len(after_select) == 1


def test_qc04_subjectivity_penalty_can_flip_ranking() -> None:
    """QC-04/FR-18: ranking must be genuinely configurable, not a hardcoded
    price-only default. With default weights (price_weight=1.0,
    subjectivity_penalty=0.0) the cheaper-but-riskier quote wins, exactly
    reproducing the original pure-premium behavior every scenario test above
    still relies on. A large enough subjectivity_penalty must be able to flip
    that ranking — proving the mechanism itself works, on synthetic quotes
    kept entirely separate from the real scenario fixtures."""
    cheap_but_risky = Quote(
        quote_id="q-cheap",
        parsed=ParsedResponse(
            filename="synthetic_cheap.txt", carrier_name="Cheap Carrier",
            named_insured="Synthetic Insured", response_date=None, response_type="QUOTE",
            premium=100_000.0,
            subjectivities=[
                Subjectivity(
                    description="4-year loss run required within 10 days",
                    materiality="material", is_dependency=False,
                )
            ],
        ),
    )
    pricier_but_clean = Quote(
        quote_id="q-clean",
        parsed=ParsedResponse(
            filename="synthetic_clean.txt", carrier_name="Clean Carrier",
            named_insured="Synthetic Insured", response_date=None, response_type="QUOTE",
            premium=110_000.0,
        ),
    )
    quotes = [cheap_but_risky, pricier_but_clean]
    as_of = date(2027, 1, 1)

    default_result = recommend(quotes, as_of)
    assert default_result.primary_quote_id == "q-cheap"

    weighted_result = recommend(quotes, as_of, price_weight=1.0, subjectivity_penalty=20_000.0)
    assert weighted_result.primary_quote_id == "q-clean"


async def test_full_pipeline_review_queue_and_audit(es_ctx, es_session) -> None:
    output = await _run(es_ctx, "scenario_04", as_of="2027-07-29")
    assert output.draft is not None and output.draft.text

    rq = DefaultReviewQueueService()
    item = await rq.enqueue(es_session, es_ctx, output, "quote_comparison")
    assert item.status is ReviewStatus.PENDING

    acted = await rq.act(es_session, es_ctx, item.id, ReviewAction.APPROVE)
    assert acted.status is ReviewStatus.APPROVED

    audit = DefaultAuditService()
    await audit.record(
        es_session, es_ctx,
        AuditEntry(
            actor="ai", who="system", what=f"decision={output.decision.outcome.value}",
            workflow="quote_comparison", tenant_id=es_ctx.tenant_id, vertical=es_ctx.vertical,
        ),
    )
    entries = await audit.query(es_session, es_ctx, {"workflow": "quote_comparison"})
    assert len(entries) == 1


# --- Live path (real Gmail, via a mocked Nango proxy) ---------------------
#
# Unlike the scenario_* tests above (static Workflow_13 fixture), these prove
# the NEW live-ingestion path added this session: real carrier-response
# emails, matched to a real submission by its real named insured, persisted
# and accumulated one at a time (FR-3), then run through the exact same
# unchanged quote_parser/comparison_engine. Same mocked-Nango-proxy technique
# as tests/test_live_nango_connector.py — no real network, no extra
# dependency.

_LIVE_SUBMISSION_ID = "SUB-QC-LIVE-01"
_LIVE_NAMED_INSURED = "Harborview Electrical Contractors LLC"
_LIVE_SUBJECT = f"RE: {_LIVE_NAMED_INSURED} - Quote Terms"

_LIVE_MESSAGES = {
    "msg-quote-1": {
        "from": "underwriting@ironcladcasualty.com",
        "date": "Tue, 27 Jul 2027 10:15:00 -0500",
        "body": (
            f"We're pleased to offer terms on {_LIVE_NAMED_INSURED}:\n\n"
            "Premium: $26,500\n"
            "General Liability: $1,000,000 / $2,000,000 aggregate\n"
            "Deductible: $2,500\n"
            "Effective Date: 09/01/2027\n"
            "Quote valid through: 08/25/2027\n"
        ),
    },
    "msg-quote-2": {
        "from": "underwriting@meridianexcess.com",
        "date": "Wed, 28 Jul 2027 09:00:00 -0500",
        "body": (
            f"We're pleased to offer terms on {_LIVE_NAMED_INSURED}:\n\n"
            "Premium: $24,000\n"
            "General Liability: $1,000,000 / $2,000,000 aggregate\n"
            "Deductible: $2,500\n"
            "Effective Date: 09/01/2027\n"
            "Quote valid through: 08/20/2027\n"
        ),
    },
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _live_quote_gmail_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/proxy/gmail/v1/users/me/messages" and request.method == "GET":
        return httpx.Response(200, json={"messages": [{"id": mid} for mid in _LIVE_MESSAGES]})
    for mid, msg in _LIVE_MESSAGES.items():
        if path == f"/proxy/gmail/v1/users/me/messages/{mid}":
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
                            {"name": "From", "value": msg["from"]},
                            {"name": "Subject", "value": _LIVE_SUBJECT},
                            {"name": "Date", "value": msg["date"]},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": _b64url(msg["body"].encode("utf-8"))},
                    }
                },
            )
    return httpx.Response(404, json={"error": f"unhandled {request.method} {path}"})


@pytest.fixture
async def live_gmail_connected(es_ctx, es_session, monkeypatch):
    """A tenant with Gmail "connected" (real Connection row), CONNECTORS_MODE
    forced to "live" for this test only (default test env is "mock" — same
    as production until an operator flips it), and every Nango proxy call
    routed to the fake handler above instead of the network.

    ``get_settings()`` is ``@lru_cache``d (core/config.py) — a bare
    ``monkeypatch.setenv`` is invisible to it once any earlier test/import
    has already called it this pytest session, so the cache must be cleared
    on both sides of the env-var flip."""
    monkeypatch.setenv("CONNECTORS_MODE", "live")
    get_settings.cache_clear()
    await upsert_connection(
        es_session, es_ctx.tenant_id, "google-mail",
        nango_connection_id="conn-live-qc", status="connected",
    )
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_live_quote_gmail_handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    yield
    get_settings.cache_clear()


async def _seed_live_named_insured(es_session, es_ctx) -> None:
    """A real persisted document is what lets ``build_live_extracted_model``
    resolve the submission's real named insured (same mechanism already used
    by Package Assembly's and Agent Communication's live paths) — needed by
    ``discover_live_carrier_responses`` to know what to search Gmail for."""
    await LocalDocumentStore().save(
        es_session, es_ctx, _LIVE_SUBMISSION_ID,
        RawDocument(
            kind=DocumentKind.OTHER,
            filename="acord_application.pdf",
            content=f"ACORD 125 - COMMERCIAL INSURANCE APPLICATION\nNamed Insured: {_LIVE_NAMED_INSURED}\n",
        ),
    )


async def test_live_inbox_discovers_real_candidate_messages(
    es_ctx, es_session, live_gmail_connected
) -> None:
    """The live-inbox picker searches Gmail by the submission's own real
    named insured — never the whole inbox — and lists whatever real messages
    come back, unparsed (the broker picks; ``/run-live`` parses)."""
    await _seed_live_named_insured(es_session, es_ctx)
    messages = await list_live_inbox(_LIVE_SUBMISSION_ID, es_ctx, es_session)
    assert {m.id for m in messages} == {"msg-quote-1", "msg-quote-2"}
    assert all(m.subject == _LIVE_SUBJECT for m in messages)


async def test_live_inbox_empty_without_real_named_insured_yet() -> None:
    """No documents persisted yet for this submission — nothing to search
    Gmail for, so the picker returns empty rather than searching blind."""
    from verticals.es.workflows.quote_comparison.live_ingestion import (
        discover_live_carrier_responses,
    )

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    ctx = Ctx(tenant_id="demo-es-empty", vertical=Vertical.ES, user_id="u-jr", role=Role.JUNIOR)
    async with maker() as session:
        session.add(Tenant(id="demo-es-empty", name="Demo E&S Empty", vertical=Vertical.ES))
        await session.commit()
        messages = await discover_live_carrier_responses(session, ctx, "SUB-NO-DOCS-YET")
    await engine.dispose()
    assert messages == []


async def test_run_live_extracts_real_single_carrier_response(
    es_ctx, es_session, live_gmail_connected
) -> None:
    """First real carrier response for this submission — proves ``run_live()``
    handles a single accumulated response safely (comparison_engine.recommend's
    ``len(viable) < 2`` branch) and extracts real, grounded terms, not a
    fixture."""
    await _seed_live_named_insured(es_session, es_ctx)
    body = RunLiveRequest(submission_id=_LIVE_SUBMISSION_ID, message_id="msg-quote-1")
    item = await run_quote_comparison_live(body, es_ctx, es_session)

    payload = item.payload
    assert payload.named_insured == _LIVE_NAMED_INSURED
    assert len(payload.quotes) == 1
    quote = payload.quotes[0]
    assert quote.carrier_name == "Ironclad Casualty Solutions"
    assert quote.premium == 26_500.0
    assert quote.source_email_reference == "response_msg-quote-1.txt"
    assert payload.output_mode in ("SINGLE_QUOTE_ROUTINE", "SINGLE_QUOTE_URGENT")


async def test_run_live_accumulates_second_real_response(
    es_ctx, es_session, live_gmail_connected
) -> None:
    """FR-3: a second real reply for the SAME submission must update the
    comparison to include both responses received so far, not just the
    latest one."""
    await _seed_live_named_insured(es_session, es_ctx)
    await run_quote_comparison_live(
        RunLiveRequest(submission_id=_LIVE_SUBMISSION_ID, message_id="msg-quote-1"),
        es_ctx, es_session,
    )
    second = await run_quote_comparison_live(
        RunLiveRequest(submission_id=_LIVE_SUBMISSION_ID, message_id="msg-quote-2"),
        es_ctx, es_session,
    )

    payload = second.payload
    assert len(payload.quotes) == 2
    carriers = {q.carrier_name for q in payload.quotes}
    assert carriers == {"Ironclad Casualty Solutions", "Meridian Excess & Surplus"}
    premiums = {q.carrier_name: q.premium for q in payload.quotes}
    assert premiums == {"Ironclad Casualty Solutions": 26_500.0, "Meridian Excess & Surplus": 24_000.0}


async def test_run_live_without_gmail_connected_raises_clear_error(
    es_ctx, es_session, monkeypatch
) -> None:
    """CONNECTORS_MODE=live but no Connection row for this tenant — must
    raise a clear, actionable error (HTTP 428), never silently fabricate a
    result."""
    from fastapi import HTTPException

    monkeypatch.setenv("CONNECTORS_MODE", "live")
    get_settings.cache_clear()
    try:
        await _seed_live_named_insured(es_session, es_ctx)
        with pytest.raises(HTTPException) as exc_info:
            await run_quote_comparison_live(
                RunLiveRequest(submission_id=_LIVE_SUBMISSION_ID, message_id="msg-quote-1"),
                es_ctx, es_session,
            )
        assert exc_info.value.status_code == 428
    finally:
        get_settings.cache_clear()
