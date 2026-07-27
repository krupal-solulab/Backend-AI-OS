"""Routes under ``/api/es/endorsement`` (docs/WORKFLOW_TEMPLATE.md step 5).
Registered by ``verticals/es/router.py`` — the one shared-file line the E&S
dev touches to mount a workflow.

The new "endorsement-confirmed" Agent Communication trigger fires
AUTOMATICALLY right here in ``/run``/``resolve-discrepancy`` — same pattern
as Binder & Issuance (a verified-clean EP-05 reconciliation is the gate,
not a broker "select" step).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, WorkflowInput
from core.common.enums import ReviewAction
from core.db import get_session
from core.llm import build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.review_queue import AuthorityError, DefaultReviewQueueService
from core.tenancy.dependencies import get_ctx
from verticals.es.agent_communication_hooks import fire_endorsement_result
from verticals.es.workflows.endorsement.schema import EndorsementRequestPayload
from verticals.es.workflows.endorsement.service import (
    DEFAULT_WORKFLOW_N,
    WORKFLOW_NAME,
    EndorsementPipeline,
)

router = APIRouter(prefix="/endorsement", tags=["es:endorsement"])

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

_RESOLUTIONS = {"accept_carrier_issuance", "flag_carrier_error"}


def _pipeline() -> EndorsementPipeline:
    return EndorsementPipeline(llm=build_llm_service(), workflow_n=DEFAULT_WORKFLOW_N)


class RunRequest(BaseModel):
    scenario_ref: str


class ResolveDiscrepancyRequest(BaseModel):
    resolution: str  # accept_carrier_issuance | flag_carrier_error


class ReviewItemOut(BaseModel):
    id: str
    submission_id: str | None
    status: str
    payload: EndorsementRequestPayload | None = None


async def _item_or_404(item_id: str, ctx: Ctx, session: AsyncSession) -> ReviewItemRow:
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == item_id, col(ReviewItemRow.tenant_id) == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no endorsement review item '{item_id}'")
    return item


async def _pkg_row_for(session: AsyncSession, item: ReviewItemRow) -> OutputPackageRow | None:
    if not item.output_package_id:
        return None
    return (
        await session.execute(
            select(OutputPackageRow).where(col(OutputPackageRow.id) == item.output_package_id)
        )
    ).scalar_one_or_none()


@router.post("/run", status_code=status.HTTP_201_CREATED)
async def run_endorsement(body: RunRequest, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    pipeline = _pipeline()
    output = await pipeline.run(ctx, WorkflowInput(source_ref=body.scenario_ref))

    review_queue = DefaultReviewQueueService()
    item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)

    await fire_endorsement_result(
        session, ctx, submission_id=item.submission_id, payload=output.payload
    )
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=EndorsementRequestPayload(**output.payload),
    )


@router.get("")
async def list_endorsement(ctx: CtxDep, session: SessionDep) -> list[ReviewItemOut]:
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
async def get_endorsement(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=EndorsementRequestPayload(**pkg.payload) if pkg and pkg.payload else None,
    )


@router.post("/{item_id}/resolve-discrepancy")
async def resolve_discrepancy(
    item_id: str, body: ResolveDiscrepancyRequest, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    """FR-15/FR-19: broker explicitly resolves an EP-05 partial-fulfillment
    discrepancy — required before the endorsement-confirmed trigger can
    fire. Workflow-owned: ``ReviewAction`` has no matching frozen value,
    same pattern as Binder & Issuance's resolve-*-discrepancy endpoints."""
    if body.resolution not in _RESOLUTIONS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"resolution must be one of {sorted(_RESOLUTIONS)}",
        )
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    if pkg is None or pkg.payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no endorsement payload for this item")

    payload = dict(pkg.payload)
    carrier_response = dict(payload.get("carrier_response") or {})
    already_fired = bool(payload.get("downstream_trigger_fired"))
    if carrier_response.get("reconciliation_status") == "DISCREPANCY_FLAGGED":
        carrier_response["reconciliation_status"] = "BROKER_RESOLVED"
    payload["carrier_response"] = carrier_response
    payload["downstream_trigger_fired"] = True

    pkg.payload = payload
    session.add(pkg)
    await session.commit()

    if not already_fired:
        await fire_endorsement_result(
            session, ctx, submission_id=item.submission_id,
            payload={**payload, "downstream_trigger_fired": True},
        )

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what=f"resolved discrepancy: {body.resolution}",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id, "resolution": body.resolution},
        ),
    )
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=EndorsementRequestPayload(**payload),
    )


async def _act(
    item_id: str, action: ReviewAction, ctx: Ctx, session: AsyncSession
) -> ReviewItemOut:
    try:
        item = await DefaultReviewQueueService().act(session, ctx, item_id, action)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what=f"action={action.value}",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    return ReviewItemOut(id=item.id, submission_id=item.submission_id, status=item.status.value)


@router.post("/{item_id}/send")
async def send(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """FR-19: "Send routine request" / "Send for underwriting review with
    context" — both map to the same generic SEND action; the framing
    difference already lives in the drafted request text itself."""
    return await _act(item_id, ReviewAction.SEND, ctx, session)


@router.post("/{item_id}/escalate")
async def escalate(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """FR-19: "Escalate appetite-unknown to carrier" — reuses the existing
    frozen ``ReviewAction.ESCALATE``, which already fits."""
    return await _act(item_id, ReviewAction.ESCALATE, ctx, session)
