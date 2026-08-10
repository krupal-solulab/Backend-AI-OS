"""Eval — Bind Order & Issuance against all 6 REAL Workflow-06 scenarios vs. their
expected_output.txt outcomes. Fixtures loaded via ``fixtures.load_scenario`` — never
hardcoded here. Scenario_03's staleness math needs a deterministic "now" — injected via
``request_override`` with ``_now`` set to a date after the dataset's own bind-time
context (09/01/2027, the effective date every scenario in this set binds toward), rather
than the real wall-clock date. Skips cleanly if the dataset isn't present on this machine.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select

import core.models  # noqa: F401  (register shared tables)
from core.common.dtos import Ctx
from core.common.enums import Role, Vertical
from core.models import ReviewItem, Tenant
from core.review_queue import AuthorityError
from verticals.mga.bind_issuance.fixtures import dataset_dir, load_scenario
from verticals.mga.bind_issuance.service import WORKFLOW, BindIssuanceService
from verticals.mga.models import MgaBindResult

pytestmark = pytest.mark.skipif(
    dataset_dir() is None,
    reason="Workflow-06 dataset not found under 'Data sets/Workflow-06/mga_bind_issuance_dataset'",
)

NOW = "2027-09-01"

EXPECTED = {
    "scenario_01": "READY",
    "scenario_02": "BLOCKED",
    "scenario_03": "BLOCKED",
    "scenario_04": "BLOCKED",
    "scenario_05": "BLOCKED",  # issuance discrepancy flagged
    "scenario_06": "READY",
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


def _with_now(scenario: str) -> dict[str, Any]:
    request = load_scenario(scenario)
    assert request is not None
    return {**request, "_now": NOW}


async def test_all_six_outcomes_match_spec(mem_session: AsyncSession, mga_ctx: Ctx) -> None:
    svc = BindIssuanceService()
    got: dict[str, str] = {}
    for ref in EXPECTED:
        detail = await svc.process(mem_session, mga_ctx, ref, request_override=_with_now(ref))
        got[ref] = detail.bindOrderStatus
    assert got == EXPECTED, got


async def test_scenario_01_clean_ready_to_bind(mem_session: AsyncSession, mga_ctx: Ctx) -> None:
    d = await BindIssuanceService().process(
        mem_session, mga_ctx, "scenario_01", request_override=_with_now("scenario_01"))
    assert d.bindOrderStatus == "READY"
    assert d.worksheetReference is not None and d.worksheetReference.premium == 19845
    assert d.authorityReconfirmation is not None
    assert d.authorityReconfirmation.outcome == "WITHIN_AUTHORITY"
    assert d.pasWriteBack.logged is True
    assert all(s.status == "cleared" for s in d.preBindSubjectivities)


async def test_scenario_02_material_subjectivity_blocks(
    mem_session: AsyncSession, mga_ctx: Ctx
) -> None:
    d = await BindIssuanceService().process(
        mem_session, mga_ctx, "scenario_02", request_override=_with_now("scenario_02"))
    assert d.bindOrderStatus == "BLOCKED"
    assert d.pasWriteBack.logged is False
    open_material = [s for s in d.preBindSubjectivities
                     if s.materiality == "material" and s.status != "cleared"]
    assert len(open_material) == 1


async def test_scenario_03_stale_worksheet_exceeds_ceiling_on_recheck(
    mem_session: AsyncSession, mga_ctx: Ctx
) -> None:
    d = await BindIssuanceService().process(
        mem_session, mga_ctx, "scenario_03", request_override=_with_now("scenario_03"))
    assert d.bindOrderStatus == "BLOCKED"
    assert d.stalenessCheck is not None
    assert d.stalenessCheck.exceedsThreshold is True
    assert d.stalenessCheck.materialUpdateLoggedSince is True
    assert d.authorityReconfirmation is not None
    assert d.authorityReconfirmation.outcome == "EXCEEDS_CEILING_REFERRAL_REQUIRED"
    assert d.authorityReconfirmation.checkedPremium == 128900
    assert d.authorityReconfirmation.referralDraftText is not None
    assert "TransGuard" in d.authorityReconfirmation.referralDraftText


async def test_scenario_04_hand_edited_premium_blocked(
    mem_session: AsyncSession, mga_ctx: Ctx
) -> None:
    d = await BindIssuanceService().process(
        mem_session, mga_ctx, "scenario_04", request_override=_with_now("scenario_04"))
    assert d.bindOrderStatus == "BLOCKED"
    assert "does NOT match" in d.rationale or "27,500" in d.rationale
    assert d.pasWriteBack.logged is False


async def test_scenario_05_discrepancy_flagged_holds_downstream(
    mem_session: AsyncSession, mga_ctx: Ctx
) -> None:
    d = await BindIssuanceService().process(
        mem_session, mga_ctx, "scenario_05", request_override=_with_now("scenario_05"))
    assert d.issuanceReconciliation.status == "DISCREPANCY_FLAGGED"
    assert len(d.issuanceReconciliation.discrepancyDetail) == 1
    disc = d.issuanceReconciliation.discrepancyDetail[0]
    assert disc.field == "deductible" and disc.bound == "1000" and disc.issued == "2500"
    # MBI-06: downstream triggers must be held while a discrepancy is unresolved
    assert d.downstreamTriggersFired.bindConfirmation is False
    assert d.downstreamTriggersFired.policyDelivered is False


async def test_scenario_06_post_bind_obligation_does_not_block(
    mem_session: AsyncSession, mga_ctx: Ctx
) -> None:
    d = await BindIssuanceService().process(
        mem_session, mga_ctx, "scenario_06", request_override=_with_now("scenario_06"))
    assert d.bindOrderStatus == "READY"
    assert len(d.postBindObligations) == 1
    obligation = d.postBindObligations[0]
    assert obligation.status == "open"
    assert obligation.reminderDaysBefore == [15, 5]


async def test_persistence_and_rbac(mem_session: AsyncSession, mga_ctx: Ctx) -> None:
    svc = BindIssuanceService()
    for ref in EXPECTED:
        await svc.process(mem_session, mga_ctx, ref, request_override=_with_now(ref))
    rows = await svc.list_rows(mem_session, mga_ctx)
    assert len(rows) == 6
    assert (await mem_session.execute(select(MgaBindResult))).scalars().all().__len__() == 6

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
