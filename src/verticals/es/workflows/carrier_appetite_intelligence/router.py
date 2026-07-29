"""Routes under ``/api/es/carrier-appetite-intelligence``
(docs/WORKFLOW_TEMPLATE.md step 5). Registered by
``verticals/es/router.py`` — the one shared-file line the E&S dev touches
to mount a workflow.

Every ``/run`` call enqueues a ``ReviewItem`` regardless of outcome — same
uniform pattern every prior workflow already uses (e.g. Market Matching
enqueues zero-match results too) — so the standard ``GET`` list already
gives full visibility into every evaluation, including suppressed ones.
This is how PRD §2.3's suppression-rate health metric stays observable
without a dedicated endpoint: computing it is a matter of listing items
and grouping by ``payload.pattern_type``, not something this router needs
to precompute.

``dismiss`` is workflow-owned (``ReviewAction`` has no matching value),
mirroring Agent Communication's ``discard`` precedent — it records that a
human reviewed a suggestion and declined to act on it, WITHOUT touching
any Carrier Appetite Profile data (none exists to touch — see
``schema.py``'s module docstring).
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
from verticals.es.workflows.carrier_appetite_intelligence.live_signal_builder import (
    discover_live_carriers,
)
from verticals.es.workflows.carrier_appetite_intelligence.schema import (
    CarrierAppetiteEvaluationPayload,
)
from verticals.es.workflows.carrier_appetite_intelligence.service import (
    DEFAULT_WORKFLOW_N,
    WORKFLOW_NAME,
    CarrierAppetiteIntelligencePipeline,
)

router = APIRouter(
    prefix="/carrier-appetite-intelligence", tags=["es:carrier-appetite-intelligence"]
)

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _pipeline() -> CarrierAppetiteIntelligencePipeline:
    return CarrierAppetiteIntelligencePipeline(
        llm=build_llm_service(), workflow_n=DEFAULT_WORKFLOW_N
    )


class RunRequest(BaseModel):
    scenario_ref: str


class ReviewItemOut(BaseModel):
    id: str
    submission_id: str | None
    status: str
    payload: CarrierAppetiteEvaluationPayload | None = None


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
            status.HTTP_404_NOT_FOUND, f"no carrier-appetite-intelligence review item '{item_id}'"
        )
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
async def run_carrier_appetite_intelligence(
    body: RunRequest, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    pipeline = _pipeline()
    output = await pipeline.run(ctx, WorkflowInput(source_ref=body.scenario_ref))

    review_queue = DefaultReviewQueueService()
    item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=CarrierAppetiteEvaluationPayload(**output.payload),
    )


@router.post("/run-live", status_code=status.HTTP_201_CREATED)
async def run_carrier_appetite_intelligence_live(
    ctx: CtxDep, session: SessionDep
) -> list[ReviewItemOut]:
    """Additive alongside ``/run`` above: evaluates every carrier with a
    real, classifiable declination signal already logged by Quote
    Comparison for this tenant — the genuine cross-workflow aggregation
    CI-01 describes, built from actual OutputPackage rows rather than the
    Workflow_18 fixture. See ``live_signal_builder.py``."""
    carriers = await discover_live_carriers(session, ctx)
    review_queue = DefaultReviewQueueService()
    items: list[ReviewItemOut] = []
    for c in carriers:
        pipeline = _pipeline()
        output = await pipeline.run_live(ctx, session, c["carrier_name"])
        item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
        items.append(
            ReviewItemOut(
                id=item.id, submission_id=item.submission_id, status=item.status.value,
                payload=CarrierAppetiteEvaluationPayload(**output.payload),
            )
        )
    return items


@router.get("")
async def list_carrier_appetite_intelligence(
    ctx: CtxDep, session: SessionDep
) -> list[ReviewItemOut]:
    rows = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.workflow) == WORKFLOW_NAME,
            )
        )
    ).scalars().all()
    out = []
    for r in rows:
        pkg = await _pkg_row_for(session, r)
        payload = CarrierAppetiteEvaluationPayload(**pkg.payload) if pkg and pkg.payload else None
        out.append(
            ReviewItemOut(
                id=r.id, submission_id=r.submission_id, status=r.status.value, payload=payload
            )
        )
    return out


@router.get("/{item_id}")
async def get_carrier_appetite_intelligence(
    item_id: str, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=CarrierAppetiteEvaluationPayload(**pkg.payload) if pkg and pkg.payload else None,
    )


@router.post("/{item_id}/approve")
async def approve(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """A human approving a GENUINE_INCONSISTENCY suggestion — records
    approval only. Per the approved plan, no mutable Carrier Appetite
    Profile store exists in this codebase, so approval does NOT itself
    change any accepted/excluded class list; a human still applies that
    change manually via whatever real profile-management path Market
    Matching eventually builds (out of scope for this PRD, per FR-6)."""
    try:
        await DefaultReviewQueueService().act(session, ctx, item_id, ReviewAction.APPROVE)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    if pkg is not None and pkg.payload is not None:
        payload = dict(pkg.payload)
        payload["status"] = "APPROVED"
        pkg.payload = payload
        session.add(pkg)
        await session.commit()

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what="approved appetite-shift suggestion",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    return ReviewItemOut(id=item.id, submission_id=item.submission_id, status=item.status.value)


@router.post("/{item_id}/dismiss")
async def dismiss(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """FR-4's suggestion-queue: dismissing a suggestion. ``ReviewAction``
    has no "dismissed" value (frozen enum), so this sets the
    workflow-owned ``payload.status`` directly — same pattern as Agent
    Communication's ``discard``."""
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    if pkg is None or pkg.payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no payload for this item")

    payload = dict(pkg.payload)
    payload["status"] = "DISMISSED"
    pkg.payload = payload
    session.add(pkg)
    await session.commit()

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what="dismissed appetite-shift suggestion",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=CarrierAppetiteEvaluationPayload(**payload),
    )
