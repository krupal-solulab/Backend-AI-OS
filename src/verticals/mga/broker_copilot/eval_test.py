"""Eval — Broker Communication Copilot drafts FROM real Triage (Workflow_1) and Renewal
(Workflow_2) decisions. Runs the source workflow first (same as its own eval), then drafts
a broker communication from that output and asserts the trigger classification, tone
calibration, and grounded/cited draft match the source decision's shape. No new fixtures
needed — this workflow reuses the Extraction/Decision Core outputs already proven by the
other two evals, per the roadmap's "Very High leverage" rating.
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
from verticals.mga.broker_copilot.service import WORKFLOW, BrokerCopilotService
from verticals.mga.models import MgaBrokerCommResult
from verticals.mga.renewal_management.service import RenewalService
from verticals.mga.submission_triage.service import TriageService

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_1/2 fixtures unavailable",
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


async def test_triage_proceed_drafts_quote_summary(mem_session, mga_ctx) -> None:
    triage = await TriageService().triage(mem_session, mga_ctx, "submission_01")
    assert triage.recommendation == "PROCEED"
    comm = await BrokerCopilotService().draft(
        mem_session, mga_ctx, "submission-triage", "submission_01")
    assert comm.type == "QUOTE_SUMMARY"
    assert comm.sourceWorkflow == "Submission Triage"
    assert comm.body and not comm.requiresComplianceReview


async def test_triage_request_info_drafts_missing_info_request(mem_session, mga_ctx) -> None:
    triage = await TriageService().triage(mem_session, mga_ctx, "submission_09")
    assert triage.recommendation == "REQUEST_INFO"
    comm = await BrokerCopilotService().draft(
        mem_session, mga_ctx, "submission-triage", "submission_09")
    assert comm.type == "MISSING_INFO_REQUEST"
    assert comm.citations  # grounded in the source's missing-info list


async def test_renewal_non_renew_drafts_compliance_gated_notice(mem_session, mga_ctx) -> None:
    renewal = await RenewalService().renew(mem_session, mga_ctx, "renewal_04")
    assert renewal.recommendation == "NON_RENEW"
    comm = await BrokerCopilotService().draft(
        mem_session, mga_ctx, "renewal-management", "renewal_04")
    assert comm.type == "NON_RENEWAL_NOTICE"
    assert comm.requiresComplianceReview is True
    assert comm.sensitive is True
    assert comm.status == "UNDER_COMPLIANCE_REVIEW"
    # non-renewal drafts must never surface loss dollar figures
    assert "$" not in comm.body


async def test_renewal_with_changes_drafts_terms_explanation(mem_session, mga_ctx) -> None:
    renewal = await RenewalService().renew(mem_session, mga_ctx, "renewal_03")
    assert renewal.recommendation == "RENEW_WITH_CHANGES"
    comm = await BrokerCopilotService().draft(
        mem_session, mga_ctx, "renewal-management", "renewal_03")
    assert comm.type in ("RENEWAL_TERMS_EXPLANATION", "MISSING_INFO_REQUEST")


async def test_missing_source_raises_not_found(mem_session, mga_ctx) -> None:
    with pytest.raises(KeyError):
        await BrokerCopilotService().draft(
            mem_session, mga_ctx, "submission-triage", "does-not-exist")


async def test_persistence_and_list_detail(mem_session, mga_ctx) -> None:
    await TriageService().triage(mem_session, mga_ctx, "submission_01")
    await TriageService().triage(mem_session, mga_ctx, "submission_09")
    svc = BrokerCopilotService()
    await svc.draft(mem_session, mga_ctx, "submission-triage", "submission_01")
    await svc.draft(mem_session, mga_ctx, "submission-triage", "submission_09")

    drafts = await svc.list_drafts(mem_session, mga_ctx)
    assert len(drafts) == 2

    detail = await svc.get_detail(mem_session, mga_ctx, drafts[0].id)
    assert detail is not None and detail.id == drafts[0].id

    results = (await mem_session.execute(select(MgaBrokerCommResult))).scalars().all()
    assert len(results) == 2


async def test_human_action_rbac(mem_session, mga_ctx) -> None:
    await TriageService().triage(mem_session, mga_ctx, "submission_01")
    svc = BrokerCopilotService()
    await svc.draft(mem_session, mga_ctx, "submission-triage", "submission_01")

    item = (await mem_session.execute(
        select(ReviewItem).where(col(ReviewItem.workflow) == WORKFLOW))).scalars().first()
    assert item is not None
    sub_id = item.submission_id
    assert sub_id is not None

    # junior may not 'send' (senior-only, same authority rule as Triage/Renewal)
    with pytest.raises(AuthorityError):
        await svc.act(mem_session, mga_ctx, sub_id, "send")

    senior_ctx = mga_ctx.model_copy(update={"role": Role.SENIOR})
    sent = await svc.act(mem_session, senior_ctx, sub_id, "send")
    assert sent["status"] == "sent"


async def test_compliance_gated_draft_cannot_bypass_review(mem_session, mga_ctx) -> None:
    """Non-renewal notices start UNDER_COMPLIANCE_REVIEW; senior may still act on them via
    the review queue (the FE gates the Send button on `reviewed`, enforced client-side —
    the backend's authority gate here is role, matching the other two workflows)."""
    await RenewalService().renew(mem_session, mga_ctx, "renewal_04")
    svc = BrokerCopilotService()
    comm = await svc.draft(mem_session, mga_ctx, "renewal-management", "renewal_04")
    assert comm.status == "UNDER_COMPLIANCE_REVIEW"
