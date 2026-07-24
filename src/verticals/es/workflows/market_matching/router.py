"""Routes under ``/api/es/market-matching`` (docs/WORKFLOW_TEMPLATE.md step 5).
Registered by ``verticals/es/router.py`` — the one shared-file line the E&S dev
touches to mount a workflow.
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
from core.documents import LocalDocumentStore
from core.extraction import DefaultExtractionService
from core.ingestion.connectors import build_connector_service
from core.llm import build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.review_queue import AuthorityError, DefaultReviewQueueService
from core.rules_engine import DefaultRulesEngine
from core.tenancy.dependencies import get_ctx
from verticals.es.agent_communication_hooks import fire_no_market_found
from verticals.es.workflows.market_matching.schema import MarketMatchingPayload
from verticals.es.workflows.market_matching.service import (
    DEFAULT_WORKFLOW_N,
    WORKFLOW_NAME,
    MarketMatchingPipeline,
)

# Relative prefix only — verticals/es/router.py (already "/api/es") includes this,
# producing the full "/api/es/market-matching" path. Do not add "/api/es" here.
router = APIRouter(prefix="/market-matching", tags=["es:market-matching"])

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _pipeline(session: AsyncSession) -> MarketMatchingPipeline:
    return MarketMatchingPipeline(
        session=session,
        connector=build_connector_service(workflow_n=DEFAULT_WORKFLOW_N),
        extraction=DefaultExtractionService(),
        rules_engine=DefaultRulesEngine(),
        llm=build_llm_service(),
        documents=LocalDocumentStore(),
        workflow_n=DEFAULT_WORKFLOW_N,
    )


class RunRequest(BaseModel):
    submission_ref: str  # e.g. "submission_01" — the fixture/Nango message id


class ReviewItemOut(BaseModel):
    id: str
    submission_id: str | None
    status: str
    payload: MarketMatchingPayload | None = None


class DocumentOut(BaseModel):
    filename: str
    kind: str
    content: str


@router.post("/run", status_code=status.HTTP_201_CREATED)
async def run_market_matching(body: RunRequest, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """Runs the full pipeline for one submission and enqueues the result for
    human review — nothing here binds/sends anything (human-in-the-loop)."""
    pipeline = _pipeline(session)
    inp = WorkflowInput(submission_id=body.submission_ref, source_ref=body.submission_ref)
    output = await pipeline.run(ctx, inp)

    review_queue = DefaultReviewQueueService()
    item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
    await fire_no_market_found(session, ctx, output)  # additive, no-throw — see module docstring
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=output.payload,
    )


@router.get("")
async def list_market_matching(ctx: CtxDep, session: SessionDep) -> list[ReviewItemOut]:
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
async def get_market_matching(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == item_id, col(ReviewItemRow.tenant_id) == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no market-matching review item '{item_id}'"
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
        id=item.id, submission_id=item.submission_id, status=item.status.value, payload=payload
    )


@router.get("/{item_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    item_id: str, ctx: CtxDep, session: SessionDep
) -> list[DocumentOut]:
    """The raw documents `ingest()` persisted via `LocalDocumentStore` for this
    submission — real fixture content, not extracted/cited fields (see
    core/extraction for that)."""
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == item_id, col(ReviewItemRow.tenant_id) == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if item is None or item.submission_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no market-matching review item '{item_id}'"
        )
    docs = await LocalDocumentStore().list_for_submission(session, ctx, item.submission_id)
    return [DocumentOut(filename=d.filename, kind=d.kind.value, content=d.content) for d in docs]


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


@router.post("/{item_id}/override")
async def override(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    return await _act(item_id, ReviewAction.OVERRIDE, ctx, session)


@router.post("/{item_id}/escalate")
async def escalate(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    return await _act(item_id, ReviewAction.ESCALATE, ctx, session)


@router.post("/{item_id}/send")
async def send(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    return await _act(item_id, ReviewAction.SEND, ctx, session)


@router.post("/{item_id}/issue")
async def issue(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    return await _act(item_id, ReviewAction.ISSUE, ctx, session)
