"""E&S Diligent Search & Compliance Documentation eval test — proves the
pipeline + router against the REAL Workflow_17 dataset (originally
``Data sets/Workflow 8/diligent_search_dataset``, copied to
``TEST_DATA_ROOT/Workflow_17/test_dataset`` per DATA_AND_FIXTURES.md).

Pytest-discovered here (not under src/verticals/es/...) — see
verticals/es/workflows/diligent_search/eval_test.py for why.

Scenario 03 is the mandatory, non-skippable release gate for this
workflow (PRD §2.3/§8): zero non-compliant document generation is the
highest-stakes success criterion in the entire vertical.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select

import core.models  # noqa: F401  (registers tables)
from core.common.dtos import Ctx, WorkflowInput
from core.common.enums import DecisionOutcome, ReviewStatus, Role, Vertical
from core.config import get_settings
from core.llm import build_llm_service
from core.models import ReviewItem as ReviewItemRow
from core.models import Tenant
from verticals.es.workflows.diligent_search.router import (
    LiveDeclinationInput,
    LiveStateInput,
    RunLiveRequest,
    RunRequest,
    approve,
    escalate,
    list_live_submissions,
    run_diligent_search,
    run_diligent_search_live,
)
from verticals.es.workflows.diligent_search.service import DiligentSearchPipeline
from verticals.es.workflows.market_matching.router import (
    RunRequest as MarketMatchingRunRequest,
)
from verticals.es.workflows.market_matching.router import run_market_matching

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_17 fixtures unavailable",
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


@pytest.fixture
def mock_connectors_mode(monkeypatch):
    """Seeding a real MM-07 stub only needs Market Matching's own fixture
    path (``submission_ref``), same as ``test_zero_match_seeds_diligent_
    search_stub`` in test_es_market_matching.py — forces mock mode for this
    setup step regardless of the environment's own CONNECTORS_MODE, so
    these tests aren't at the mercy of a real (unmocked) Nango call."""
    monkeypatch.setenv("CONNECTORS_MODE", "mock")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _pipeline() -> DiligentSearchPipeline:
    return DiligentSearchPipeline(llm=build_llm_service())


async def _run(ctx: Ctx, scenario_ref: str):
    pipeline = _pipeline()
    return await pipeline.run(ctx, WorkflowInput(source_ref=scenario_ref))


async def test_scenario_01_ready_sufficient_evidence(es_ctx) -> None:
    """3 of 3 required declinations on file, all written — a compliant
    document is generated, grounded only in the actual declination
    records."""
    output = await _run(es_ctx, "scenario_01")
    payload = output.payload
    assert payload["overall_status"] == "COMPLETE"
    assert output.decision.outcome is DecisionOutcome.PROCEED
    state = payload["state_determinations"][0]
    assert state["state"] == "Oregon"
    assert state["requirement_status"] == "REQUIRED"
    assert state["sufficiency_status"] == "SUFFICIENT"
    assert state["document_generated"] is True
    assert state["generated_document_text"]
    assert len(state["declinations_on_file"]) == 3


async def test_scenario_02_exempt_explicitly_logged(es_ctx) -> None:
    """Export-list exemption must be its own distinct, explicitly-logged
    determination — never indistinguishable from "missing documentation"."""
    output = await _run(es_ctx, "scenario_02")
    payload = output.payload
    assert payload["overall_status"] == "COMPLETE"
    state = payload["state_determinations"][0]
    assert state["state"] == "Texas"
    assert state["requirement_status"] == "EXEMPT"
    assert state["exemption_basis"] is not None and "export list" in state["exemption_basis"]
    assert state["sufficiency_status"] == "NOT_APPLICABLE"
    assert state["document_generated"] is False
    assert state["generated_document_text"] is None


async def test_scenario_03_blocked_insufficient_evidence_no_document(es_ctx) -> None:
    """RELEASE GATE: only 2 of 3 declinations on file, one verbal-only.
    Must BLOCK and generate ZERO document text — a wrong affidavit here
    would be a potentially fraudulent record, the PRD's own top risk."""
    output = await _run(es_ctx, "scenario_03")
    payload = output.payload
    assert payload["overall_status"] == "BLOCKED"
    assert output.decision.outcome is DecisionOutcome.REQUEST_INFO
    state = payload["state_determinations"][0]
    assert state["state"] == "Florida"
    assert state["requirement_status"] == "REQUIRED"
    assert state["sufficiency_status"] == "INSUFFICIENT"
    assert state["document_generated"] is False
    assert state["generated_document_text"] is None
    assert "1 more" in state["gap_detail"]
    assert "Admitted Carrier B" in state["gap_detail"]


async def test_scenario_04_partial_multistate_checklist(es_ctx) -> None:
    """8-state risk: TN/GA confirmed requiring standard diligent search
    (evidence not yet submitted), FL's hedged export-list note routes to
    PENDING_DETERMINATION (not auto-exempt, per FR-7), and the 5 unlisted
    states are explicitly flagged incomplete — never a single collapsed
    verdict."""
    output = await _run(es_ctx, "scenario_04")
    payload = output.payload
    assert payload["overall_status"] == "PARTIAL"
    states = {s["state"]: s for s in payload["state_determinations"]}
    assert len(states) == 8

    assert states["TN"]["requirement_status"] == "REQUIRED"
    assert states["TN"]["sufficiency_status"] == "NOT_APPLICABLE"
    assert states["GA"]["requirement_status"] == "REQUIRED"

    # The core FR-7 judgment call: a hedged, account-specific export-list note
    # must NOT auto-resolve to EXEMPT.
    assert states["FL"]["requirement_status"] == "PENDING_DETERMINATION"
    assert states["FL"]["exemption_basis"] is None

    for code in ("NC", "SC", "VA", "AL", "MS"):
        assert states[code]["requirement_status"] == "PENDING_DETERMINATION"
        assert states[code]["gap_detail"]

    assert all(not s["document_generated"] for s in states.values())


async def test_run_and_approve(es_ctx_senior, es_session) -> None:
    body = RunRequest(scenario_ref="scenario_01")
    item = await run_diligent_search(body, es_ctx_senior, es_session)
    assert item.status == ReviewStatus.PENDING.value
    approved = await approve(item.id, es_ctx_senior, es_session)
    assert approved.status == ReviewStatus.APPROVED.value


async def test_escalate_pending_determination(es_ctx_senior, es_session) -> None:
    body = RunRequest(scenario_ref="scenario_04")
    item = await run_diligent_search(body, es_ctx_senior, es_session)
    escalated = await escalate(item.id, es_ctx_senior, es_session)
    assert escalated.status == ReviewStatus.ESCALATED.value


# --- Live path: real MM-07 stub -> broker-entered real per-state data -----
#
# Market Matching's own zero-match fixture (submission_06) genuinely fires
# MM-07 and seeds a real, linked diligent_search review item stub via
# verticals/es/diligent_search_hooks.py — no mocked Gmail needed here, since
# the hook itself is already real regardless of how Market Matching sourced
# the submission (fixture ref or a real live message id).


async def test_live_submissions_discovers_real_mm07_stub(
    es_ctx, es_session, mock_connectors_mode
) -> None:
    await run_market_matching(
        MarketMatchingRunRequest(submission_ref="submission_06"), es_ctx, es_session
    )
    stubs = await list_live_submissions(es_ctx, es_session)
    assert len(stubs) == 1
    assert stubs[0].submission_id == "submission_06"


async def test_run_live_mixed_real_per_state_outcomes(
    es_ctx, es_session, mock_connectors_mode
) -> None:
    """Gap-fill: a broker/compliance person enters real per-state facts and
    real declination records for a real MM-07-seeded submission — never
    inferred or invented. DS-01..DS-04's strict, unmodified logic decides
    each state exactly as it does for the fixture path, just fed different
    (real, human-supplied) inputs."""
    await run_market_matching(
        MarketMatchingRunRequest(submission_ref="submission_06"), es_ctx, es_session
    )
    stubs = await list_live_submissions(es_ctx, es_session)
    item_id = stubs[0].item_id

    result = await run_diligent_search_live(
        item_id,
        RunLiveRequest(
            submission_id="submission_06",
            named_insured="Live Test Insured LLC",
            states=[
                LiveStateInput(state="TX", status="exempt", export_list_note="On export list"),
                LiveStateInput(
                    state="CA", status="required", admitted_declinations_required=1,
                    declinations=[
                        LiveDeclinationInput(
                            carrier="Acme Admitted Co", date="2027-01-05", written_evidence=True
                        )
                    ],
                ),
                LiveStateInput(
                    state="NY", status="required", admitted_declinations_required=1,
                    declinations=[
                        LiveDeclinationInput(
                            carrier="Verbal Only Co", date="2027-01-06", written_evidence=False
                        )
                    ],
                ),
                LiveStateInput(state="FL", status="pending"),
            ],
        ),
        es_ctx, es_session,
    )
    payload = result.payload
    assert payload.named_insured == "Live Test Insured LLC"
    states = {s.state: s for s in payload.state_determinations}

    assert states["TX"].requirement_status == "EXEMPT"
    assert states["TX"].exemption_basis == "On export list"

    assert states["CA"].sufficiency_status == "SUFFICIENT"
    assert states["CA"].document_generated is True
    assert states["CA"].generated_document_text

    assert states["NY"].sufficiency_status == "INSUFFICIENT"
    assert states["NY"].document_generated is False
    assert states["NY"].generated_document_text is None

    assert states["FL"].requirement_status == "PENDING_DETERMINATION"

    assert payload.overall_status == "BLOCKED"  # NY's confirmed gap takes priority


async def test_run_live_missing_required_count_never_defaults_to_zero(
    es_ctx, es_session, mock_connectors_mode
) -> None:
    """Real bug found via live usage (not caught by planning): a "Required"
    state with no declination count entered was being silently treated as
    "0 required" — trivially satisfied by ANY declination, even one. For
    this workflow's own zero-tolerance gate, that's exactly the silent
    false-pass it exists to prevent. Missing count must route through
    DS-01's own null-safety to PENDING_DETERMINATION, never a fabricated
    SUFFICIENT/document_generated."""
    await run_market_matching(
        MarketMatchingRunRequest(submission_ref="submission_06"), es_ctx, es_session
    )
    stubs = await list_live_submissions(es_ctx, es_session)
    item_id = stubs[0].item_id

    result = await run_diligent_search_live(
        item_id,
        RunLiveRequest(
            submission_id="submission_06",
            named_insured="Regression Test Insured LLC",
            states=[
                LiveStateInput(
                    state="CA", status="required", admitted_declinations_required=None,
                    declinations=[
                        LiveDeclinationInput(carrier="Written", date="01/01/2001", written_evidence=True)
                    ],
                ),
            ],
        ),
        es_ctx, es_session,
    )
    ca = result.payload.state_determinations[0]
    assert ca.requirement_status == "PENDING_DETERMINATION"
    assert ca.sufficiency_status == "NOT_APPLICABLE"
    assert ca.document_generated is False
    assert ca.generated_document_text is None


async def test_run_live_updates_same_item_not_duplicated(
    es_ctx, es_session, mock_connectors_mode
) -> None:
    await run_market_matching(
        MarketMatchingRunRequest(submission_ref="submission_06"), es_ctx, es_session
    )
    stubs = await list_live_submissions(es_ctx, es_session)
    item_id = stubs[0].item_id

    await run_diligent_search_live(
        item_id,
        RunLiveRequest(
            submission_id="submission_06",
            named_insured="Live Test Insured LLC",
            states=[LiveStateInput(state="TX", status="exempt", export_list_note="Exempt")],
        ),
        es_ctx, es_session,
    )

    rows = (
        await es_session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.tenant_id) == es_ctx.tenant_id,
                col(ReviewItemRow.workflow) == "diligent_search",
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == item_id

    # A completed item no longer shows up as a pending stub.
    stubs_after = await list_live_submissions(es_ctx, es_session)
    assert stubs_after == []
