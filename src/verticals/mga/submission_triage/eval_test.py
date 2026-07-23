"""Eval — run Submission Triage against all 10 REAL Workflow_1 fixtures and assert each
recommendation + key flags match the dataset's expected-outcome spec
(Workflow_1/test_dataset/README.md + Validation_Rules_Test_Dataset.md).

Fixtures are loaded via the shared loader/connector — never hardcoded here.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select

import core.models  # noqa: F401  (register shared tables)
from core.common.dtos import Ctx
from core.common.enums import Role, Vertical
from core.config import get_settings
from core.models import ReviewItem, Tenant
from core.review_queue import AuthorityError
from verticals.mga.models import MgaAppetiteResult
from verticals.mga.submission_triage.service import WORKFLOW, TriageService

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_1 fixtures unavailable",
)


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

EXPECTED = {
    "submission_01": "PROCEED",
    "submission_02": "REQUEST_INFO",
    "submission_03": "DECLINE",
    "submission_04": "REQUEST_INFO",
    "submission_05": "REQUEST_INFO",
    "submission_06": "PROCEED",
    "submission_07": "REQUEST_INFO",
    "submission_08": "REQUEST_INFO",  # manual review
    "submission_09": "REQUEST_INFO",
    "submission_10": "PROCEED",
}
ALL = list(EXPECTED)


async def _run_all(session, ctx):
    svc = TriageService()
    return {mid: await svc.triage(session, ctx, mid) for mid in ALL}


async def test_all_ten_recommendations_match_spec(mem_session, mga_ctx) -> None:
    details = await _run_all(mem_session, mga_ctx)
    got = {mid: d.recommendation for mid, d in details.items()}
    assert got == EXPECTED, got


async def test_clean_proceed_has_grounded_narrative(mem_session, mga_ctx) -> None:
    svc = TriageService()
    d = await svc.triage(mem_session, mga_ctx, "submission_01")
    assert d.recommendation == "PROCEED"
    assert d.hardRulePassed and not d.failedRules
    assert d.narrative and d.citations  # LLM ran + cited
    assert d.loss.years == 5


async def test_decline_short_circuits_before_narrative(mem_session, mga_ctx) -> None:
    svc = TriageService()
    d = await svc.triage(mem_session, mga_ctx, "submission_03")
    assert d.recommendation == "DECLINE"
    assert not d.hardRulePassed
    assert {"HR-01", "HR-02"} <= set(d.failedRules)  # double failure per spec design intent
    assert not d.citations  # narrative suppressed → no LLM citations
    assert "Declined on hard rule" in d.narrative
    assert d.loss.trend == "worsening"


async def test_submission_08_manual_review(mem_session, mga_ctx) -> None:
    svc = TriageService()
    d = await svc.triage(mem_session, mga_ctx, "submission_08")
    assert d.recommendation == "REQUEST_INFO"
    assert "EC-01" in d.failedRules
    assert d.meta.lowConfidence  # degraded-scan fields flagged
    assert "manual review" in d.narrative.lower()
    assert any(f.name == "Manual review" for f in d.factors)


async def test_submission_09_missing_financials_and_sov_limit(mem_session, mga_ctx) -> None:
    svc = TriageService()
    d = await svc.triage(mem_session, mga_ctx, "submission_09")
    assert d.recommendation == "REQUEST_INFO"
    assert any(m.item == "Financial statement" for m in d.missingInfo)
    assert any(c.label == "SOV vs requested limit" and c.status == "fail" for c in d.consistency)


async def test_consistency_flags_04_and_05(mem_session, mga_ctx) -> None:
    svc = TriageService()
    d4 = await svc.triage(mem_session, mga_ctx, "submission_04")
    assert any(c.label == "Revenue consistency" and c.status == "fail" for c in d4.consistency)
    d5 = await svc.triage(mem_session, mga_ctx, "submission_05")
    assert any(c.label == "Loss disclosure consistency" and c.status == "fail"
               for c in d5.consistency)


async def test_persistence_and_list_detail(mem_session, mga_ctx) -> None:
    svc = TriageService()
    await _run_all(mem_session, mga_ctx)

    rows = await svc.list_rows(mem_session, mga_ctx)
    assert len(rows) == 10
    proceed_row = next(r for r in rows if r.recommendation == "PROCEED")
    detail = await svc.get_detail(mem_session, mga_ctx, proceed_row.id)
    assert detail is not None and detail.recommendation == "PROCEED"

    appetite_rows = (await mem_session.execute(select(MgaAppetiteResult))).scalars().all()
    assert len(appetite_rows) == 10


async def test_human_action_rbac(mem_session, mga_ctx) -> None:
    svc = TriageService()
    detail_sub = "submission_01"
    await svc.triage(mem_session, mga_ctx, detail_sub)
    item = (await mem_session.execute(
        select(ReviewItem).where(col(ReviewItem.workflow) == WORKFLOW))).scalars().first()
    assert item is not None
    sub_id = item.submission_id
    assert sub_id is not None

    # junior approving within cap is allowed
    res = await svc.act(mem_session, mga_ctx, sub_id, "approve", amount=50_000)
    assert res["status"] == "approved"

    # junior approving over JUNIOR_PREMIUM_CAP is blocked
    with pytest.raises(AuthorityError):
        await svc.act(mem_session, mga_ctx, sub_id, "approve", amount=500_000)

    # senior can escalate/send
    senior_ctx = mga_ctx.model_copy(update={"role": Role.SENIOR})
    _ = Vertical  # keep import used
    sent = await svc.act(mem_session, senior_ctx, sub_id, "send")
    assert sent["status"] == "sent"
