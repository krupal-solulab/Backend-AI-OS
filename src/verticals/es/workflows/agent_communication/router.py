"""Routes under ``/api/es/agent-communication`` (docs/WORKFLOW_TEMPLATE.md step
5). Registered by ``verticals/es/router.py`` — the one shared-file line the
E&S dev touches to mount a workflow.

Two frozen-enum gaps this router works around WITHOUT touching core/common:
- ``ReviewAction`` has no "compliance sign-off" value, so clearing a No Market
  Found draft's compliance gate (FR-8/FR-16) is a dedicated, router-owned
  endpoint that flips the workflow's own ``payload.requires_compliance_review``
  flag directly, rather than going through ``review_queue.act()``.
- ``ReviewStatus`` has no "discarded" value (FR-15's third action), so discard
  is handled the same way, via ``payload.status``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, WorkflowInput
from core.common.enums import ReviewAction, ReviewStatus, Role
from core.db import get_session
from core.llm import build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.review_queue import AuthorityError, DefaultReviewQueueService
from core.tenancy.dependencies import get_ctx
from verticals.es.workflows.agent_communication.schema import DraftCommunicationOut
from verticals.es.workflows.agent_communication.service import (
    WORKFLOW_NAME,
    AgentCommunicationPipeline,
)

router = APIRouter(prefix="/agent-communication", tags=["es:agent-communication"])

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

_ACTION_PAYLOAD_STATUS = {ReviewAction.APPROVE: "APPROVED", ReviewAction.SEND: "SENT"}


def _pipeline(session: AsyncSession) -> AgentCommunicationPipeline:
    return AgentCommunicationPipeline(session=session, llm=build_llm_service())


class RunRequest(BaseModel):
    trigger: dict[str, Any]  # shaped like a trigger_XX/trigger_input.json object


class ReviewItemOut(BaseModel):
    id: str
    submission_id: str | None
    status: str
    deduplicated: bool = False
    payload: DraftCommunicationOut | None = None


async def _item_or_404(item_id: str, ctx: Ctx, session: AsyncSession) -> ReviewItemRow:
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == item_id, col(ReviewItemRow.tenant_id) == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no agent-communication review item '{item_id}'"
        )
    return item


async def _payload_for(session: AsyncSession, item: ReviewItemRow) -> dict[str, Any] | None:
    if not item.output_package_id:
        return None
    pkg = (
        await session.execute(
            select(OutputPackageRow).where(col(OutputPackageRow.id) == item.output_package_id)
        )
    ).scalar_one_or_none()
    return pkg.payload if pkg else None


async def _pkg_row_for(session: AsyncSession, item: ReviewItemRow) -> OutputPackageRow | None:
    if not item.output_package_id:
        return None
    return (
        await session.execute(
            select(OutputPackageRow).where(col(OutputPackageRow.id) == item.output_package_id)
        )
    ).scalar_one_or_none()


async def _find_pending_duplicate(
    session: AsyncSession, ctx: Ctx, submission_id: str | None, trigger_type: str | None,
    carrier_name: str | None,
) -> tuple[ReviewItemRow, dict[str, Any]] | None:
    """FR-5: no duplicate draft for an UNRESOLVED existing trigger."""
    if not submission_id:
        return None
    stmt = (
        select(ReviewItemRow)
        .where(
            col(ReviewItemRow.tenant_id) == ctx.tenant_id,
            col(ReviewItemRow.workflow) == WORKFLOW_NAME,
            col(ReviewItemRow.submission_id) == submission_id,
            col(ReviewItemRow.status) == ReviewStatus.PENDING,
        )
        .order_by(col(ReviewItemRow.created_at).desc())
    )
    for item in (await session.execute(stmt)).scalars().all():
        payload = await _payload_for(session, item)
        if not payload or payload.get("trigger_type") != trigger_type:
            continue
        if carrier_name and payload.get("carrier_name") not in (None, carrier_name):
            continue
        return item, payload
    return None


async def _find_prior_followup(
    session: AsyncSession, ctx: Ctx, submission_id: str | None, carrier_name: str | None,
) -> ReviewItemRow | None:
    """FR-12: at most one NO_RESPONSE_FOLLOWUP EVER per original request — a
    stricter check than FR-5's (this looks at every status, not just pending)."""
    if not submission_id:
        return None
    stmt = select(ReviewItemRow).where(
        col(ReviewItemRow.tenant_id) == ctx.tenant_id,
        col(ReviewItemRow.workflow) == WORKFLOW_NAME,
        col(ReviewItemRow.submission_id) == submission_id,
    )
    for item in (await session.execute(stmt)).scalars().all():
        payload = await _payload_for(session, item)
        if payload and payload.get("trigger_type") == "NO_RESPONSE_FOLLOWUP" and (
            not carrier_name or payload.get("carrier_name") == carrier_name
        ):
            return item
    return None


@router.post("/run", status_code=status.HTTP_201_CREATED)
async def run_agent_communication(
    body: RunRequest, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    """Drafts one communication from a trigger object (PRD FR-1/FR-2: a Market
    Matching / Package Assembly output object, or a manually-logged trigger) —
    never sends anything (human-in-the-loop, FR-20)."""
    trigger = body.trigger
    trigger_type = trigger.get("trigger_type")
    submission_id = trigger.get("submission_id")
    carrier_name = trigger.get("carrier_name")

    if trigger_type == "NO_RESPONSE_FOLLOWUP":
        prior = await _find_prior_followup(session, ctx, submission_id, carrier_name)
        if prior is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "a follow-up has already been generated for this request "
                "(FR-12: at most one follow-up per original communication)",
            )

    dup = await _find_pending_duplicate(session, ctx, submission_id, trigger_type, carrier_name)
    if dup is not None:
        dup_item, dup_payload = dup
        return ReviewItemOut(
            id=dup_item.id, submission_id=dup_item.submission_id, status=dup_item.status.value,
            deduplicated=True, payload=DraftCommunicationOut(**dup_payload),
        )

    pipeline = _pipeline(session)
    inp = WorkflowInput(submission_id=submission_id, params={"trigger_data": trigger})
    output = await pipeline.run(ctx, inp)

    review_queue = DefaultReviewQueueService()
    item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=DraftCommunicationOut(**output.payload),
    )


@router.get("")
async def list_agent_communication(ctx: CtxDep, session: SessionDep) -> list[ReviewItemOut]:
    rows = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.workflow) == WORKFLOW_NAME,
            )
        )
    ).scalars().all()
    return [
        ReviewItemOut(id=r.id, submission_id=r.submission_id, status=r.status.value) for r in rows
    ]


@router.get("/{item_id}")
async def get_agent_communication(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    item = await _item_or_404(item_id, ctx, session)
    payload = await _payload_for(session, item)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=DraftCommunicationOut(**payload) if payload else None,
    )


async def _assert_not_compliance_gated(
    action: ReviewAction, item_id: str, ctx: Ctx, session: AsyncSession
) -> None:
    """FR-8/FR-16: a compliance-gated draft must never get the same one-click
    approve/send affordance as a normal draft, until POST /{id}/compliance-clear
    runs — enforced here, not left as a FE-only convention."""
    if action not in (ReviewAction.APPROVE, ReviewAction.SEND):
        return
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == item_id, col(ReviewItemRow.tenant_id) == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        return
    payload = await _payload_for(session, item)
    if payload and payload.get("requires_compliance_review") is True:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"draft requires compliance review before '{action.value}' — "
            f"see POST /{item_id}/compliance-clear",
        )


async def _act(
    item_id: str, action: ReviewAction, ctx: Ctx, session: AsyncSession
) -> ReviewItemOut:
    await _assert_not_compliance_gated(action, item_id, ctx, session)
    review_queue = DefaultReviewQueueService()
    try:
        item = await review_queue.act(session, ctx, item_id, action)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    if action in _ACTION_PAYLOAD_STATUS:
        # review_queue.act() returns a transport DTO (no output_package_id) —
        # re-fetch the ORM row to reach the payload it doesn't carry.
        item_row = await _item_or_404(item_id, ctx, session)
        pkg_row = await _pkg_row_for(session, item_row)
        if pkg_row is not None and pkg_row.payload:
            payload = dict(pkg_row.payload)
            payload["status"] = _ACTION_PAYLOAD_STATUS[action]
            if action is ReviewAction.SEND:
                payload["sent_timestamp"] = datetime.now(UTC).isoformat()
            pkg_row.payload = payload
            session.add(pkg_row)
            await session.commit()

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what=f"action={action.value}",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    return ReviewItemOut(id=item.id, submission_id=item.submission_id, status=item.status.value)


@router.post("/{item_id}/approve")
async def approve(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    return await _act(item_id, ReviewAction.APPROVE, ctx, session)


@router.post("/{item_id}/send")
async def send(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    return await _act(item_id, ReviewAction.SEND, ctx, session)


@router.post("/{item_id}/edit")
async def edit(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """Audit-only, no status change (FR-17 logs the edit for the feedback loop;
    computing a real edit-distance needs the FE's submitted edited body, which
    is a follow-on enhancement — same simplification package_assembly's own
    ``/edit`` made)."""
    item = await _item_or_404(item_id, ctx, session)
    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what="edited",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    return ReviewItemOut(id=item.id, submission_id=item.submission_id, status=item.status.value)


@router.post("/{item_id}/discard")
async def discard(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """FR-15's third one-click action. ``ReviewStatus`` has no frozen
    "discarded" value, so this sets the workflow-owned ``payload.status``
    instead of routing through ``review_queue.act()`` — same pattern as
    ``compliance-clear`` below."""
    item = await _item_or_404(item_id, ctx, session)
    pkg_row = await _pkg_row_for(session, item)
    if pkg_row is None or pkg_row.payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no draft payload for this item")

    payload = dict(pkg_row.payload)
    payload["status"] = "DISCARDED"
    pkg_row.payload = payload
    session.add(pkg_row)
    await session.commit()

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what="discarded",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=DraftCommunicationOut(**payload),
    )


@router.post("/{item_id}/compliance-clear")
async def compliance_clear(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """FR-8/FR-16: clears the compliance-review gate on a No Market Found
    draft, once the design partner's compliance/legal function has actually
    signed off (RA-TN-06 — this endpoint does not decide THAT question, it
    only records that a human with authority marked it resolved). Senior/admin
    only, mirroring review_queue's existing gating for consequential actions.
    ``ReviewAction`` has no "compliance sign-off" value (frozen enum), so this
    is a dedicated, router-owned endpoint rather than a ``review_queue.act()``
    call — see module docstring."""
    if ctx.role is Role.JUNIOR:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "compliance-clear requires senior/admin role"
        )
    item = await _item_or_404(item_id, ctx, session)
    pkg_row = await _pkg_row_for(session, item)
    if pkg_row is None or pkg_row.payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no draft payload for this item")

    payload = dict(pkg_row.payload)
    payload["requires_compliance_review"] = False
    if payload.get("status") == "UNDER_COMPLIANCE_REVIEW":
        payload["status"] = "DRAFT"
    pkg_row.payload = payload
    session.add(pkg_row)
    await session.commit()

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what="compliance_clear",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=DraftCommunicationOut(**payload),
    )
