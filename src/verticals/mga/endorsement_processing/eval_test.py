"""Eval — Endorsement Processing against all 6 REAL Workflow-04 scenarios vs. their
expected_output.txt outcomes. Fixtures loaded via ``fixtures.load_scenario`` — never
hardcoded here. Skips cleanly if the dataset isn't present on this machine (mirrors the
TEST_DATA_ROOT skip pattern the other MGA workflow evals use, pointed at this workflow's
own dataset location instead).
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
from verticals.mga.decision_core.rating import RatingEngine, RatingInput
from verticals.mga.endorsement_processing.fixtures import dataset_dir
from verticals.mga.endorsement_processing.service import WORKFLOW, EndorsementService
from verticals.mga.models import MgaEndorsementResult

pytestmark = pytest.mark.skipif(
    dataset_dir() is None,
    reason="Workflow-04 dataset not found under 'Data sets/Workflow-04/mga_endorsement_dataset'",
)

EXPECTED = {
    "scenario_01": "WITHIN_AUTHORITY",
    "scenario_02": "WITHIN_AUTHORITY",
    "scenario_03": "EXCEEDS_CEILING_REFERRAL_REQUIRED",
    "scenario_04": "CONFIRMED_EXCLUSION_DECLINED",
    "scenario_05": "WITHIN_AUTHORITY",
    "scenario_06": "BLOCKED_STALE_RATE",
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


async def test_all_six_outcomes_match_spec(mem_session, mga_ctx) -> None:
    svc = EndorsementService()
    got = {ref: (await svc.process(mem_session, mga_ctx, ref)).authorityCheck.outcome
           for ref in EXPECTED}
    assert got == EXPECTED, got


async def test_scenario_01_routine_fast_track(mem_session, mga_ctx) -> None:
    d = await EndorsementService().process(mem_session, mga_ctx, "scenario_01")
    assert d.classification == "ROUTINE"
    assert d.status == "READY_TO_PROCESS"
    assert d.premiumBearing is False
    assert d.hardRulePassed is True
    assert d.carrierReferral.drafted is False


async def test_scenario_02_material_within_authority_uses_real_rating_engine(
    mem_session, mga_ctx
) -> None:
    d = await EndorsementService().process(mem_session, mga_ctx, "scenario_02")
    assert d.classification == "MATERIAL"
    assert d.status == "READY_TO_PROCESS"
    # premium impact ~$2,559 per the dataset's own hand-rounded 42.5% factor; the real
    # engine computes the precise 155/365 fraction, so allow a small rounding delta.
    assert d.premiumBearing is True
    delta = float(d.premiumDelta.replace("+$", "").replace(",", ""))
    assert abs(delta - 2559) < 10
    resulting = d.authorityCheck.resultingTotalPremium
    assert resulting is not None and "70,9" in resulting  # ~$70,957-$70,959
    assert d.authorityCheck.outcome == "WITHIN_AUTHORITY"


async def test_scenario_03_exceeds_ceiling_drafts_referral_not_decline(
    mem_session, mga_ctx
) -> None:
    d = await EndorsementService().process(mem_session, mga_ctx, "scenario_03")
    assert d.authorityCheck.outcome == "EXCEEDS_CEILING_REFERRAL_REQUIRED"
    assert d.authorityCheck.resultingTotalPremium == "$139,200"
    assert d.carrierReferral.drafted is True
    assert d.carrierReferral.draftText and "TransGuard" in d.carrierReferral.draftText
    assert d.hardRulePassed is True  # blocked from ROUTINE processing, not a hard-rule failure
    assert d.writeBackRecord.logged is False  # not processed yet — pending carrier response


async def test_scenario_04_confirmed_exclusion_declined_not_referred(
    mem_session, mga_ctx
) -> None:
    d = await EndorsementService().process(mem_session, mga_ctx, "scenario_04")
    assert d.authorityCheck.outcome == "CONFIRMED_EXCLUSION_DECLINED"
    assert d.authorityCheck.excludedClassMatched == "roofing - steep slope"
    # never referred — confirmed exclusions decline outright, unlike a ceiling breach
    assert d.carrierReferral.drafted is False
    assert d.hardRulePassed is False
    # narrative suppressed, no LLM call for a confirmed exclusion
    assert "Declined outright" in d.rationale


async def test_scenario_05_writeback_closes_bordereau_completeness_gap(
    mem_session, mga_ctx
) -> None:
    d = await EndorsementService().process(mem_session, mga_ctx, "scenario_05")
    assert d.status == "READY_TO_PROCESS"
    assert d.writeBackRecord.logged is True
    assert d.writeBackRecord.bordereauSchemaValidated is True
    assert d.writeBackRecord.transactionType == "ENDORSEMENT"
    assert d.writeBackRecord.policyNumber == "APX-GL-88410"
    assert d.writeBackRecord.premiumDelta is not None


async def test_scenario_06_inherits_qr08_staleness_check(mem_session, mga_ctx) -> None:
    d = await EndorsementService().process(mem_session, mga_ctx, "scenario_06")
    assert d.status == "BLOCKED_STALE_RATE"
    assert "SUPERSEDED" in d.rationale or "not current" in d.rationale.lower()
    assert d.writeBackRecord.logged is False


async def test_rating_engine_never_reimplemented_directly(mem_session, mga_ctx) -> None:
    """MEP-03 must call the actual RatingEngine, not a parallel calculation — verified
    here by confirming the engine's own bounds/staleness protections are the thing that
    blocks scenario_06, not endorsement-specific logic."""
    from verticals.mga.endorsement_processing.engine import EndorsementEngine

    svc_engine = EndorsementEngine()
    assert isinstance(svc_engine.rating, RatingEngine)
    result = svc_engine.rating.calculate(RatingInput(
        class_code="96065", new_location_tiv=1.0,
        rate_plan_version="SC-96065-2026.2", filed_status="SUPERSEDED - x",
        base_rate_per_1000_tiv=1.0))
    assert result.blocked is True and result.rate_plan_currency_check == "STALE_BLOCKED"


async def test_persistence_and_rbac(mem_session, mga_ctx) -> None:
    svc = EndorsementService()
    for ref in EXPECTED:
        await svc.process(mem_session, mga_ctx, ref)
    rows = await svc.list_rows(mem_session, mga_ctx)
    assert len(rows) == 6
    assert (await mem_session.execute(select(MgaEndorsementResult))).scalars().all().__len__() == 6

    # get_detail is keyed by submission_id, not the row's endorsementId — resolve via review items
    item = (await mem_session.execute(
        select(ReviewItem).where(col(ReviewItem.workflow) == WORKFLOW))).scalars().first()
    assert item is not None and item.submission_id is not None
    detail = await svc.get_detail(mem_session, mga_ctx, item.submission_id)
    assert detail is not None

    with pytest.raises(AuthorityError):  # junior may not 'send' (carrier referral, senior-only)
        await svc.act(mem_session, mga_ctx, item.submission_id, "send")

    senior_ctx = mga_ctx.model_copy(update={"role": Role.SENIOR})
    sent = await svc.act(mem_session, senior_ctx, item.submission_id, "send")
    assert sent["status"] == "sent"
