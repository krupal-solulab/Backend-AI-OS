"""Review queue persistence + RBAC/authority-gated actions."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.auth import can_approve
from core.common.dtos import Ctx
from core.common.dtos import OutputPackage as OutputPackageDTO
from core.common.dtos import ReviewItem as ReviewItemDTO
from core.common.enums import ReviewAction, ReviewStatus, Role
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow

# Actions restricted to senior/admin regardless of amount.
_SENIOR_ACTIONS = {ReviewAction.OVERRIDE, ReviewAction.SEND, ReviewAction.ISSUE}

_ACTION_STATUS = {
    ReviewAction.APPROVE: ReviewStatus.APPROVED,
    ReviewAction.OVERRIDE: ReviewStatus.OVERRIDDEN,
    ReviewAction.ESCALATE: ReviewStatus.ESCALATED,
    ReviewAction.SEND: ReviewStatus.SENT,
    ReviewAction.ISSUE: ReviewStatus.ISSUED,
}


class AuthorityError(Exception):
    """Raised when a user's role/authority does not permit the requested action."""


class ReviewQueueService(Protocol):
    async def enqueue(
        self, session: AsyncSession, ctx: Ctx, output: OutputPackageDTO, workflow: str
    ) -> ReviewItemDTO: ...
    async def act(
        self,
        session: AsyncSession,
        ctx: Ctx,
        item_id: str,
        action: ReviewAction,
        amount: float | None = None,
    ) -> ReviewItemDTO: ...


class DefaultReviewQueueService:
    async def enqueue(
        self, session: AsyncSession, ctx: Ctx, output: OutputPackageDTO, workflow: str
    ) -> ReviewItemDTO:
        pkg = OutputPackageRow(
            tenant_id=ctx.tenant_id,
            submission_id=output.submission_id or "",
            workflow=workflow,
            payload=output.payload or None,
        )
        session.add(pkg)
        await session.flush()

        item = ReviewItemRow(
            tenant_id=ctx.tenant_id,
            submission_id=output.submission_id,
            output_package_id=pkg.id,
            workflow=workflow,
            status=ReviewStatus.PENDING,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return ReviewItemDTO(
            id=item.id, submission_id=item.submission_id, workflow=workflow,
            status=item.status, output=output,
        )

    async def act(
        self,
        session: AsyncSession,
        ctx: Ctx,
        item_id: str,
        action: ReviewAction,
        amount: float | None = None,
    ) -> ReviewItemDTO:
        item = (
            await session.execute(
                select(ReviewItemRow).where(
                    col(ReviewItemRow.id) == item_id,
                    col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise KeyError(f"review item '{item_id}' not found for tenant")

        self._authorize(ctx.role, action, amount)
        item.status = _ACTION_STATUS[action]
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return ReviewItemDTO(
            id=item.id, submission_id=item.submission_id, workflow=item.workflow,
            status=item.status, output=None,
        )

    @staticmethod
    def _authorize(role: Role, action: ReviewAction, amount: float | None) -> None:
        if action is ReviewAction.ESCALATE:
            return  # anyone may escalate
        if action in _SENIOR_ACTIONS and role is Role.JUNIOR:
            raise AuthorityError(f"role '{role}' may not '{action.value}' (senior/admin only)")
        if action is ReviewAction.APPROVE and not can_approve(role, amount):
            raise AuthorityError(
                f"junior approval over authority cap (amount={amount}); must escalate"
            )
