"""Routes under ``/api/es/diligent-search`` (docs/WORKFLOW_TEMPLATE.md
step 5). Registered by ``verticals/es/router.py`` — the one shared-file
line the E&S dev touches to mount a workflow.

No new workflow-owned action endpoint is needed here, unlike several
prior workflows: the PRD's own end-to-end step 6 ("broker reviews the
per-state checklist, gathers missing evidence, escalates ambiguous
determinations to compliance/legal") is fully covered by the existing
``ReviewAction.APPROVE``/``ESCALATE`` — approve when satisfied with the
determination, escalate an ambiguous (PENDING_DETERMINATION) state for
legal review. No new Agent Communication trigger, no scheduled job, and
no cross-workflow re-invocation apply to this workflow either — re-scanned
against all 8 FRs; none mandates broker-facing communication drafting or
re-invoking another pipeline.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx, WorkflowInput
from core.common.enums import ReviewAction
from core.db import get_session
from core.llm import build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.review_queue import AuthorityError, DefaultReviewQueueService
from core.tenancy.dependencies import get_ctx
from verticals.es.workflows.diligent_search.schema import ComplianceRecordPayload
from verticals.es.workflows.diligent_search.service import (
    DEFAULT_WORKFLOW_N,
    WORKFLOW_NAME,
    DiligentSearchPipeline,
)

router = APIRouter(prefix="/diligent-search", tags=["es:diligent-search"])

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _pipeline() -> DiligentSearchPipeline:
    return DiligentSearchPipeline(llm=build_llm_service(), workflow_n=DEFAULT_WORKFLOW_N)


class RunRequest(BaseModel):
    scenario_ref: str


class ReviewItemOut(BaseModel):
    id: str
    submission_id: str | None
    status: str
    payload: ComplianceRecordPayload | None = None


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
            status.HTTP_404_NOT_FOUND, f"no diligent-search review item '{item_id}'"
        )
    return item


async def _payload_for(
    session: AsyncSession, item: ReviewItemRow
) -> ComplianceRecordPayload | None:
    if not item.output_package_id:
        return None
    pkg = (
        await session.execute(
            select(OutputPackageRow).where(col(OutputPackageRow.id) == item.output_package_id)
        )
    ).scalar_one_or_none()
    return ComplianceRecordPayload(**pkg.payload) if pkg and pkg.payload else None


@router.post("/run", status_code=status.HTTP_201_CREATED)
async def run_diligent_search(body: RunRequest, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    pipeline = _pipeline()
    output = await pipeline.run(ctx, WorkflowInput(source_ref=body.scenario_ref))

    review_queue = DefaultReviewQueueService()
    item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=ComplianceRecordPayload(**output.payload),
    )


@router.get("")
async def list_diligent_search(ctx: CtxDep, session: SessionDep) -> list[ReviewItemOut]:
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
async def get_diligent_search(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    item = await _item_or_404(item_id, ctx, session)
    payload = await _payload_for(session, item)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value, payload=payload
    )


async def _act(
    item_id: str, action: ReviewAction, ctx: Ctx, session: AsyncSession
) -> ReviewItemOut:
    review_queue = DefaultReviewQueueService()
    try:
        item = await review_queue.act(session, ctx, item_id, action)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return ReviewItemOut(id=item.id, submission_id=item.submission_id, status=item.status.value)


@router.post("/{item_id}/approve")
async def approve(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    return await _act(item_id, ReviewAction.APPROVE, ctx, session)


@router.post("/{item_id}/escalate")
async def escalate(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """FR-7: escalates an ambiguous/account-specific PENDING_DETERMINATION
    state (e.g. Scenario 04's Florida) to compliance/legal review."""
    return await _act(item_id, ReviewAction.ESCALATE, ctx, session)
