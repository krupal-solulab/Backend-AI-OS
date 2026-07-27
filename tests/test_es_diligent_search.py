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
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.common.dtos import Ctx, WorkflowInput
from core.common.enums import DecisionOutcome, ReviewStatus, Role, Vertical
from core.config import get_settings
from core.llm import build_llm_service
from core.models import Tenant
from verticals.es.workflows.diligent_search.router import (
    RunRequest,
    approve,
    escalate,
    run_diligent_search,
)
from verticals.es.workflows.diligent_search.service import DiligentSearchPipeline

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
