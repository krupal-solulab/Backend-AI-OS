"""Eval — Appetite Governance & Audit Trail against all 6 REAL Workflow-07 scenarios vs.
their expected_output.txt outcomes. Fixtures loaded via ``fixtures.load_scenario`` —
never hardcoded here. Skips cleanly if the dataset isn't present on this machine.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select

import core.models  # noqa: F401  (register shared tables)
from core.common.dtos import Ctx
from core.common.enums import Role, Vertical
from core.models import ReviewItem, Tenant
from core.review_queue import AuthorityError
from verticals.mga.appetite_governance.fixtures import dataset_dir
from verticals.mga.appetite_governance.service import WORKFLOW, GovernanceService
from verticals.mga.models import MgaGovernanceResult

pytestmark = pytest.mark.skipif(
    dataset_dir() is None,
    reason="Workflow-07 dataset not found under 'Data sets/Workflow-07/"
    "appetite_governance_dataset'",
)

EXPECTED = {
    "scenario_01": "COMPLETE_CLEAN",
    "scenario_02": "FLAGGED",
    "scenario_03": "SUGGESTED",
    "scenario_04": "READY",
    "scenario_05": "PARTIAL",
    "scenario_06": "INFORMATIONAL",
}


@pytest.fixture
async def mem_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(Tenant(id="demo-mga", name="Demo MGA", vertical=Vertical.MGA))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def mga_ctx() -> Ctx:
    return Ctx(tenant_id="demo-mga", vertical=Vertical.MGA, user_id="u-jr", role=Role.JUNIOR)


async def test_all_six_outcomes_match_spec(mem_session: AsyncSession, mga_ctx: Ctx) -> None:
    svc = GovernanceService()
    got: dict[str, str] = {}
    for ref in EXPECTED:
        detail = await svc.process(mem_session, mga_ctx, ref)
        got[ref] = detail.status
    assert got == EXPECTED, got


async def test_scenario_01_clean_baseline(mem_session: AsyncSession, mga_ctx: Ctx) -> None:
    d = await GovernanceService().process(mem_session, mga_ctx, "scenario_01")
    assert d.status == "COMPLETE_CLEAN"
    assert d.decisionTrail.status == "COMPLETE"
    assert len(d.decisionTrail.decisions) == 4
    assert len(d.ruleVersionDriftFindings) == 0
    assert len(d.governanceSuggestionQueue) == 0


async def test_scenario_02_rule_drift_flagged_not_actioned(
    mem_session: AsyncSession, mga_ctx: Ctx
) -> None:
    d = await GovernanceService().process(mem_session, mga_ctx, "scenario_02")
    assert d.status == "FLAGGED"
    assert len(d.ruleVersionDriftFindings) == 1
    finding = d.ruleVersionDriftFindings[0]
    assert finding.policyNumber == "APX-GL-96065"
    assert finding.stillQualifies is False
    # AG-07: routes to the human-reviewed queue, never auto-actioned
    assert len(d.governanceSuggestionQueue) == 1
    assert d.governanceSuggestionQueue[0].findingType == "RULE_DRIFT"
    assert d.governanceSuggestionQueue[0].status == "PENDING_REVIEW"


async def test_scenario_03_override_pattern_volume_and_documentation(
    mem_session: AsyncSession, mga_ctx: Ctx
) -> None:
    d = await GovernanceService().process(mem_session, mga_ctx, "scenario_03")
    assert d.status == "SUGGESTED"
    by_uw = {f.underwriter: f for f in d.overridePatternFindings}
    assert by_uw["T. Nguyen"].flagged is True
    assert by_uw["T. Nguyen"].overrideCount == 4
    assert by_uw["T. Nguyen"].undocumentedCount == 2
    assert by_uw["T. Nguyen"].suggestion is not None
    # the suggestion must describe the documentation gap, never characterize judgment
    assert "documented reason" in by_uw["T. Nguyen"].suggestion
    assert by_uw["M. Chen"].flagged is False
    assert by_uw["M. Chen"].suggestion is None


async def test_scenario_04_audit_report_strictly_grounded(
    mem_session: AsyncSession, mga_ctx: Ctx
) -> None:
    d = await GovernanceService().process(mem_session, mga_ctx, "scenario_04")
    assert d.status == "READY"
    assert d.auditReport is not None
    assert d.auditReport.triageDecisions == 312
    assert d.auditReport.renewalDecisions == 89
    assert d.auditReport.bindDecisions == 201
    assert d.auditReport.endorsementDecisions == 47
    assert d.auditReport.authorityCeilingBreachesReferred == 6
    assert d.auditReport.authorityCeilingBreachesApproved == 5
    assert d.auditReport.authorityCeilingBreachesDeclined == 1
    assert d.auditReport.groundingStatement != ""


async def test_scenario_05_gap_explicitly_flagged_never_smoothed(
    mem_session: AsyncSession, mga_ctx: Ctx
) -> None:
    d = await GovernanceService().process(mem_session, mga_ctx, "scenario_05")
    assert d.status == "PARTIAL"
    assert d.decisionTrail.status == "PARTIAL"
    assert len(d.decisionTrail.gaps) == 1
    gap = d.decisionTrail.gaps[0]
    assert gap.dateRange == "may"
    assert "OUTAGE" in gap.reason


async def test_scenario_06_portfolio_concentration_low_volume_caveat(
    mem_session: AsyncSession, mga_ctx: Ctx
) -> None:
    d = await GovernanceService().process(mem_session, mga_ctx, "scenario_06")
    assert d.status == "INFORMATIONAL"
    assert len(d.portfolioConcentrationFindings) == 1
    finding = d.portfolioConcentrationFindings[0]
    assert finding.accountsNearCeiling == 2
    assert finding.totalAccountsInSegment == 4
    assert finding.lowVolumeFlag is True  # 4 accounts is well below the low-volume threshold


async def test_persistence_and_rbac(mem_session: AsyncSession, mga_ctx: Ctx) -> None:
    svc = GovernanceService()
    for ref in EXPECTED:
        await svc.process(mem_session, mga_ctx, ref)
    rows = await svc.list_rows(mem_session, mga_ctx)
    assert len(rows) == 6
    assert (await mem_session.execute(select(MgaGovernanceResult))).scalars().all().__len__() == 6

    item = (await mem_session.execute(
        select(ReviewItem).where(col(ReviewItem.workflow) == WORKFLOW))).scalars().first()
    assert item is not None and item.submission_id is not None
    detail = await svc.get_detail(mem_session, mga_ctx, item.submission_id)
    assert detail is not None

    with pytest.raises(AuthorityError):  # junior may not 'send' (senior-only)
        await svc.act(mem_session, mga_ctx, item.submission_id, "send")

    senior_ctx = mga_ctx.model_copy(update={"role": Role.SENIOR})
    sent = await svc.act(mem_session, senior_ctx, item.submission_id, "send")
    assert sent["status"] == "sent"
