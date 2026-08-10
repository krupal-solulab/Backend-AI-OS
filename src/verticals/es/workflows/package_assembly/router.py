"""Routes under ``/api/es/package-assembly`` (docs/WORKFLOW_TEMPLATE.md step
5). Registered by ``verticals/es/router.py`` — the one shared-file line the
E&S dev touches to mount a workflow.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, WorkflowInput
from core.common.enums import ReviewAction
from core.db import get_session
from core.extraction import DefaultExtractionService
from core.llm import build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.review_queue import AuthorityError, DefaultReviewQueueService
from core.tenancy.dependencies import get_ctx
from verticals.es.agent_communication_hooks import fire_package_assembly_result
from verticals.es.workflows.package_assembly.live_ingestion import discover_live_carrier_ids
from verticals.es.workflows.package_assembly.scenario_loader import all_carrier_ids, load_scenario
from verticals.es.workflows.package_assembly.schema import PackageAssemblyPayload
from verticals.es.workflows.package_assembly.service import (
    DEFAULT_WORKFLOW_N,
    WORKFLOW_NAME,
    PackageAssemblyPipeline,
)

router = APIRouter(prefix="/package-assembly", tags=["es:package-assembly"])

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _pipeline() -> PackageAssemblyPipeline:
    """A fresh pipeline instance per carrier pass — required, since ingest()
    stashes per-run state on `self` (see service.py's module docstring)."""
    return PackageAssemblyPipeline(
        extraction=DefaultExtractionService(),
        llm=build_llm_service(),
        workflow_n=DEFAULT_WORKFLOW_N,
    )


class RunRequest(BaseModel):
    scenario_ref: str  # e.g. "scenario_01"
    carrier_id: str | None = None  # omit to assemble ALL selected carriers (FR-2/FR-23)


class RunFromMarketMatchingRequest(BaseModel):
    market_matching_review_item_id: str
    carrier_id: str | None = None  # omit to assemble ALL matched carriers (FR-2/FR-23)


class ReviewItemOut(BaseModel):
    id: str
    submission_id: str | None
    carrier_id: str | None = None
    status: str
    payload: PackageAssemblyPayload | None = None


async def _run_one_carrier(ctx: Ctx, scenario_ref: str, carrier_id: str) -> tuple[str, Any]:
    """The slow, per-carrier work (extraction lookup + LLM draft) — touches
    only `ctx`, never the shared AsyncSession, so callers may run several of
    these concurrently (FR-23) and only need to serialize the DB writes that
    follow."""
    pipeline = _pipeline()
    inp = WorkflowInput(source_ref=scenario_ref, params={"carrier_id": carrier_id})
    output = await pipeline.run(ctx, inp)
    return carrier_id, output




@router.post("/run", status_code=status.HTTP_201_CREATED)
async def run_package_assembly(
    body: RunRequest, ctx: CtxDep, session: SessionDep
) -> list[ReviewItemOut]:
    """Runs one independent assembly pass per selected carrier (FR-2/FR-4:
    never one shared result reused across carriers) and enqueues each as its
    own, independently reviewable ReviewItem (FR-18). Per FR-23, a
    multi-carrier selection generates concurrently rather than waiting on
    each carrier's LLM call in turn — only the DB writes below stay
    sequential, since an AsyncSession isn't safe to share across concurrent
    coroutines."""
    scenario = load_scenario(DEFAULT_WORKFLOW_N, body.scenario_ref)
    carrier_ids = [body.carrier_id] if body.carrier_id else all_carrier_ids(scenario)

    results = await asyncio.gather(
        *(_run_one_carrier(ctx, body.scenario_ref, carrier_id) for carrier_id in carrier_ids)
    )

    review_queue = DefaultReviewQueueService()
    audit = DefaultAuditService()
    items: list[ReviewItemOut] = []

    for carrier_id, output in results:
        item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
        await fire_package_assembly_result(session, ctx, output)  # additive, no-throw hook
        await audit.record(
            session, ctx,
            AuditEntry(
                actor="ai", who="system",
                what=f"package generated: status={output.decision.outcome.value}",
                workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
                detail={"carrier_id": carrier_id, "scenario_ref": body.scenario_ref},
            ),
        )
        items.append(
            ReviewItemOut(
                id=item.id, submission_id=item.submission_id, carrier_id=carrier_id,
                status=item.status.value, payload=output.payload,
            )
        )
    return items


@router.post("/run-from-market-matching", status_code=status.HTTP_201_CREATED)
async def run_package_assembly_from_market_matching(
    body: RunFromMarketMatchingRequest, ctx: CtxDep, session: SessionDep
) -> list[ReviewItemOut]:
    """Additive alongside ``/run`` above: assembles a real package per
    carrier from an ACTUAL Market Matching review item instead of a
    Workflow_11 fixture. See ``live_ingestion.py``. Runs carriers
    sequentially (unlike ``/run``'s ``asyncio.gather``) since each pass
    reads real data through the one shared ``AsyncSession``, which isn't
    safe to touch from concurrent coroutines."""
    carrier_ids = (
        [body.carrier_id]
        if body.carrier_id
        else await discover_live_carrier_ids(session, ctx, body.market_matching_review_item_id)
    )

    review_queue = DefaultReviewQueueService()
    audit = DefaultAuditService()
    items: list[ReviewItemOut] = []

    for carrier_id in carrier_ids:
        pipeline = _pipeline()
        output = await pipeline.run_live(
            ctx, session, body.market_matching_review_item_id, carrier_id
        )
        item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
        await fire_package_assembly_result(session, ctx, output)  # additive, no-throw hook
        await audit.record(
            session, ctx,
            AuditEntry(
                actor="ai", who="system",
                what=f"package generated (live): status={output.decision.outcome.value}",
                workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
                detail={
                    "carrier_id": carrier_id,
                    "market_matching_review_item_id": body.market_matching_review_item_id,
                },
            ),
        )
        items.append(
            ReviewItemOut(
                id=item.id, submission_id=item.submission_id, carrier_id=carrier_id,
                status=item.status.value, payload=output.payload,
            )
        )
    return items


@router.get("")
async def list_package_assembly(ctx: CtxDep, session: SessionDep) -> list[ReviewItemOut]:
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
async def get_package_assembly(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == item_id, col(ReviewItemRow.tenant_id) == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no package-assembly review item '{item_id}'"
        )
    payload = None
    if item.output_package_id:
        pkg = (
            await session.execute(
                select(OutputPackageRow).where(col(OutputPackageRow.id) == item.output_package_id)
            )
        ).scalar_one_or_none()
        payload = pkg.payload if pkg else None
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id,
        carrier_id=(payload or {}).get("carrier_id") if payload else None,
        status=item.status.value, payload=payload,
    )


async def _assert_not_blocked_for(
    action: ReviewAction, item_id: str, ctx: Ctx, session: AsyncSession
) -> None:
    """FR-10: a BLOCKED package must never get the same one-click
    ready-to-send affordance as READY/READY_WITH_GAP — enforced here, not
    left as a FE-only convention, for approve/send specifically."""
    if action not in (ReviewAction.APPROVE, ReviewAction.SEND):
        return
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == item_id, col(ReviewItemRow.tenant_id) == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if item is None or not item.output_package_id:
        return
    pkg = (
        await session.execute(
            select(OutputPackageRow).where(col(OutputPackageRow.id) == item.output_package_id)
        )
    ).scalar_one_or_none()
    if pkg and pkg.payload and pkg.payload.get("status") == "BLOCKED":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"package is BLOCKED — resolve blocking_items before '{action.value}'",
        )


async def _act(
    item_id: str, action: ReviewAction, ctx: Ctx, session: AsyncSession
) -> ReviewItemOut:
    await _assert_not_blocked_for(action, item_id, ctx, session)
    review_queue = DefaultReviewQueueService()
    try:
        item = await review_queue.act(session, ctx, item_id, action)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    # FR-21: every broker action logged for the feedback loop.
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


@router.post("/{item_id}/edit")
async def edit(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """FR-20's "Edit" action — logged like any other broker action; the
    generic ReviewQueueService has no dedicated "edited" status, so this
    records the audit trail (FR-21) without changing ReviewStatus."""
    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what="action=edit",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == item_id, col(ReviewItemRow.tenant_id) == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no package-assembly review item '{item_id}'"
        )
    return ReviewItemOut(id=item.id, submission_id=item.submission_id, status=item.status.value)


@router.post("/{item_id}/send")
async def mark_sent(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """FR-20's "Mark as sent" — a manual log only; this workflow never
    transmits anything automatically (PRD Section 2.2)."""
    return await _act(item_id, ReviewAction.SEND, ctx, session)
