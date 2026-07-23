"""Review queue — RBAC + JUNIOR_PREMIUM_CAP authority enforcement."""

from __future__ import annotations

import pytest

from core.common.dtos import Ctx, Decision, OutputPackage
from core.common.enums import DecisionOutcome, ReviewAction, ReviewStatus, Role, Vertical
from core.review_queue import AuthorityError, DefaultReviewQueueService


def _ctx(role: Role) -> Ctx:
    return Ctx(tenant_id="demo-mga", vertical=Vertical.MGA, user_id=f"u-{role.value}", role=role)


async def _enqueue(session, ctx) -> str:
    pkg = OutputPackage(
        submission_id="submission_01",
        decision=Decision(outcome=DecisionOutcome.PROCEED),
        payload={"premium": 500000},
    )
    item = await DefaultReviewQueueService().enqueue(session, ctx, pkg, "submission_triage")
    return item.id


async def test_junior_approve_within_cap(mem_session) -> None:
    ctx = _ctx(Role.JUNIOR)
    item_id = await _enqueue(mem_session, ctx)
    rq = DefaultReviewQueueService()
    result = await rq.act(mem_session, ctx, item_id, ReviewAction.APPROVE, amount=100_000)
    assert result.status is ReviewStatus.APPROVED


async def test_junior_over_cap_is_blocked(mem_session) -> None:
    ctx = _ctx(Role.JUNIOR)
    item_id = await _enqueue(mem_session, ctx)
    rq = DefaultReviewQueueService()
    with pytest.raises(AuthorityError):
        await rq.act(mem_session, ctx, item_id, ReviewAction.APPROVE, amount=500_000)


async def test_junior_cannot_issue_but_can_escalate(mem_session) -> None:
    ctx = _ctx(Role.JUNIOR)
    rq = DefaultReviewQueueService()
    item_id = await _enqueue(mem_session, ctx)
    with pytest.raises(AuthorityError):
        await rq.act(mem_session, ctx, item_id, ReviewAction.ISSUE)
    escalated = await rq.act(mem_session, ctx, item_id, ReviewAction.ESCALATE)
    assert escalated.status is ReviewStatus.ESCALATED


async def test_senior_can_issue(mem_session) -> None:
    ctx = _ctx(Role.SENIOR)
    rq = DefaultReviewQueueService()
    item_id = await _enqueue(mem_session, ctx)
    result = await rq.act(mem_session, ctx, item_id, ReviewAction.ISSUE)
    assert result.status is ReviewStatus.ISSUED
