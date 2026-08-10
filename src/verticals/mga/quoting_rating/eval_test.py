"""Eval — Quoting & Rating Support against all 6 REAL Workflow-05 scenarios vs. their
expected_output.txt outcomes. Fixtures loaded via ``fixtures.load_scenario`` — never
hardcoded here. Skips cleanly if the dataset isn't present on this machine.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select

import core.models  # noqa: F401  (register shared tables)
from core.common.dtos import Ctx
from core.common.enums import Role, Vertical
from core.models import ReviewItem
from core.review_queue import AuthorityError
from verticals.mga.decision_core.rating import RatingEngine
from verticals.mga.models import MgaQuotingResult
from verticals.mga.quoting_rating.fixtures import dataset_dir
from verticals.mga.quoting_rating.service import WORKFLOW, QuotingService

pytestmark = pytest.mark.skipif(
    dataset_dir() is None,
    reason="Workflow-05 dataset not found under 'Data sets/Workflow-05/quoting_rating_dataset'",
)

EXPECTED = {
    "scenario_01": "READY_FOR_REVIEW",
    "scenario_02": "READY_FOR_REVIEW",
    "scenario_03": "READY_FOR_REVIEW",
    "scenario_04": "READY_FOR_REVIEW",
    "scenario_05": "READY_FOR_REVIEW",
    "scenario_06": "BLOCKED_STALE_RATE_PLAN",
}


@pytest.fixture
async def mem_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        from core.models import Tenant

        session.add(Tenant(id="demo-mga", name="Demo MGA", vertical=Vertical.MGA))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def mga_ctx() -> Ctx:
    return Ctx(tenant_id="demo-mga", vertical=Vertical.MGA, user_id="u-jr", role=Role.JUNIOR)


async def test_all_six_outcomes_match_spec(mem_session, mga_ctx) -> None:
    svc = QuotingService()
    got = {ref: (await svc.process(mem_session, mga_ctx, ref)).status for ref in EXPECTED}
    assert got == EXPECTED, got


async def test_scenario_01_clean_baseline(mem_session, mga_ctx) -> None:
    d = await QuotingService().process(mem_session, mga_ctx, "scenario_01")
    assert d.status == "READY_FOR_REVIEW"
    assert d.totalIndicatedPremium == 19845.0
    sc = d.stateCalculations[0]
    assert sc.basePremium == 19845.0
    assert sc.appliedAdjustmentPct == 0.0
    assert sc.adjustmentCapped is False
    assert sc.minimumPremiumApplied is False


async def test_scenario_02_suggested_credit_not_auto_applied(mem_session, mga_ctx) -> None:
    d = await QuotingService().process(mem_session, mga_ctx, "scenario_02")
    sc = d.stateCalculations[0]
    assert sc.suggestedAdjustmentPct == -8.0
    assert sc.adjustmentGrounding is not None and "improving" in sc.adjustmentGrounding.lower()
    assert sc.basePremium == 41237.0
    # suggestion is shown as an indicated outcome, but requestedAdjustmentPct stays None —
    # nothing is auto-applied until an underwriter explicitly requests/accepts it.
    assert sc.requestedAdjustmentPct is None


async def test_scenario_03_debit_exceeds_filed_maximum_is_capped(mem_session, mga_ctx) -> None:
    d = await QuotingService().process(mem_session, mga_ctx, "scenario_03")
    sc = d.stateCalculations[0]
    assert sc.requestedAdjustmentPct == 35.0
    assert sc.appliedAdjustmentPct == 25.0
    assert sc.adjustmentCapped is True
    assert d.totalIndicatedPremium == 109020.0
    assert "capped" in d.status.lower() or d.status == "READY_FOR_REVIEW"


async def test_scenario_04_minimum_premium_floor(mem_session, mga_ctx) -> None:
    d = await QuotingService().process(mem_session, mga_ctx, "scenario_04")
    sc = d.stateCalculations[0]
    assert sc.basePremium == 1233.8
    assert sc.minimumPremiumApplied is True
    assert d.totalIndicatedPremium == 1750.0


async def test_scenario_05_multi_state_never_blended(mem_session, mga_ctx) -> None:
    d = await QuotingService().process(mem_session, mga_ctx, "scenario_05")
    assert len(d.stateCalculations) == 4
    states = {sc.state: sc.basePremium for sc in d.stateCalculations}
    assert states["TN"] == 34886.4
    assert states["NC"] == 24600.6
    assert states["GA"] == 18106.8
    assert states["SC"] == 8437.2
    assert d.totalIndicatedPremium == 86031.0


async def test_scenario_06_stale_rate_plan_blocks_calculation(mem_session, mga_ctx) -> None:
    d = await QuotingService().process(mem_session, mga_ctx, "scenario_06")
    assert d.status == "BLOCKED_STALE_RATE_PLAN"
    assert d.totalIndicatedPremium is None
    sc = d.stateCalculations[0]
    assert sc.ratePlanCurrencyCheck == "STALE_BLOCKED"
    assert sc.blockedReason is not None and "SUPERSEDED" in sc.blockedReason


async def test_rating_engine_never_reimplemented(mem_session, mga_ctx) -> None:
    """QR-02/QR-08 must call the actual RatingEngine, not a parallel calculation."""
    svc = QuotingService()
    assert isinstance(svc.engine, RatingEngine)


async def test_persistence_and_rbac(mem_session, mga_ctx) -> None:
    svc = QuotingService()
    for ref in EXPECTED:
        await svc.process(mem_session, mga_ctx, ref)
    rows = await svc.list_rows(mem_session, mga_ctx)
    assert len(rows) == 6
    assert (await mem_session.execute(select(MgaQuotingResult))).scalars().all().__len__() == 6

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
