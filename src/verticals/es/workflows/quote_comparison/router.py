"""Routes under ``/api/es/quote-comparison`` (docs/WORKFLOW_TEMPLATE.md step
5). Registered by ``verticals/es/router.py`` — the one shared-file line the
E&S dev touches to mount a workflow.

FR-20's downstream handoff to Agent Communication fires from
``/{item_id}/select/{quote_id}`` here, NOT from ``/run`` — per the PRD's own
framing (FR-23: "broker marks which quote(s) to present... which feeds
FR-20's downstream trigger"), the broker's selection is the trigger, not
Quote Comparison's recommendation output by itself. See
``verticals/es/agent_communication_hooks.py``'s module docstring.
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
from core.common.enums import ReviewAction
from core.db import get_session
from core.ingestion.connectors import ConnectorNotConnectedError
from core.llm import build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.review_queue import AuthorityError, DefaultReviewQueueService
from core.tenancy.dependencies import get_ctx
from verticals.es.agent_communication_hooks import fire_quote_comparison_result
from verticals.es.workflows.quote_comparison.comparison_engine import recompute_urgency_from_payload
from verticals.es.workflows.quote_comparison.live_ingestion import (
    discover_live_carrier_responses,
    save_live_response,
)
from verticals.es.workflows.quote_comparison.schema import ComparisonPayload
from verticals.es.workflows.quote_comparison.service import (
    DEFAULT_WORKFLOW_N,
    WORKFLOW_NAME,
    QuoteComparisonPipeline,
)

router = APIRouter(prefix="/quote-comparison", tags=["es:quote-comparison"])

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _pipeline() -> QuoteComparisonPipeline:
    return QuoteComparisonPipeline(llm=build_llm_service(), workflow_n=DEFAULT_WORKFLOW_N)


class RunRequest(BaseModel):
    scenario_ref: str  # e.g. "scenario_02"
    as_of: str | None = None  # ISO date override — fixture/test determinism only


class ReviewItemOut(BaseModel):
    id: str
    submission_id: str | None
    status: str
    payload: ComparisonPayload | None = None


class LiveInboxMessageOut(BaseModel):
    id: str
    subject: str


class RunLiveRequest(BaseModel):
    submission_id: str  # real submission (16-hex-char Gmail-derived id)
    message_id: str  # real Gmail message id, picked from /live-inbox


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
            status.HTTP_404_NOT_FOUND, f"no quote-comparison review item '{item_id}'"
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
async def run_quote_comparison(body: RunRequest, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """Runs the comparison/recommendation pipeline for one scenario/
    submission's carrier responses. Never fires Agent Communication itself —
    see module docstring."""
    pipeline = _pipeline()
    inp = WorkflowInput(
        source_ref=body.scenario_ref,
        params={"as_of": body.as_of} if body.as_of else {},
    )
    output = await pipeline.run(ctx, inp)

    review_queue = DefaultReviewQueueService()
    item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=ComparisonPayload(**output.payload),
    )


@router.get("/live-inbox")
async def list_live_inbox(
    submission_id: str, ctx: CtxDep, session: SessionDep
) -> list[LiveInboxMessageOut]:
    """Real Gmail messages that could be a carrier response for this real
    submission — matched by its real named insured in the subject line (see
    ``live_ingestion.discover_live_carrier_responses``). Additive alongside
    the fixture-scenario ``/run`` above; requires Gmail connected +
    CONNECTORS_MODE=live."""
    try:
        messages = await discover_live_carrier_responses(session, ctx, submission_id)
    except ConnectorNotConnectedError as exc:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            f"Connect Gmail in Settings first ({exc.provider} not connected)",
        ) from exc
    return [LiveInboxMessageOut(**m) for m in messages]


@router.post("/run-live", status_code=status.HTTP_201_CREATED)
async def run_quote_comparison_live(
    body: RunLiveRequest, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    """Additive alongside ``/run`` above: persists the picked real carrier-
    response email, then re-runs the comparison against EVERY real response
    accumulated so far for this submission (FR-3) — never just the latest
    one. Never fires Agent Communication itself — see module docstring."""
    try:
        await save_live_response(session, ctx, body.submission_id, body.message_id)
    except ConnectorNotConnectedError as exc:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            f"Connect Gmail in Settings first ({exc.provider} not connected)",
        ) from exc

    pipeline = _pipeline()
    output = await pipeline.run_live(ctx, session, body.submission_id)

    review_queue = DefaultReviewQueueService()
    item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=ComparisonPayload(**output.payload),
    )


@router.get("")
async def list_quote_comparison(ctx: CtxDep, session: SessionDep) -> list[ReviewItemOut]:
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
async def get_quote_comparison(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """QC-07/FR-14: recomputes urgency against TODAY on every read — a quote
    that looked fine at ingestion needs to re-check as its validity window
    approaches (see comparison_engine.recompute_urgency_from_payload's
    docstring for why this is a read-time overlay, not a scheduled job)."""
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    payload: dict[str, Any] | None = None
    if pkg and pkg.payload:
        payload = recompute_urgency_from_payload(pkg.payload, datetime.now(UTC).date())
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=ComparisonPayload(**payload) if payload else None,
    )


@router.post("/{item_id}/select/{quote_id}")
async def select_quote(
    item_id: str, quote_id: str, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    """FR-23: the broker marks which quote to present to the retail agent —
    for MULTI_OPTION this is which of the options; for SINGLE_RECOMMENDATION/
    SINGLE_QUOTE_* it confirms the (only) recommended one. This is what
    fires FR-20's downstream Agent Communication handoff (never ``/run``
    itself — see module docstring)."""
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    if pkg is None or pkg.payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no comparison payload for this item")

    quote = next((q for q in pkg.payload.get("quotes", []) if q.get("quote_id") == quote_id), None)
    if quote is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"quote '{quote_id}' not among this item's quotes_considered"
        )

    try:
        acted = await DefaultReviewQueueService().act(session, ctx, item_id, ReviewAction.APPROVE)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    payload = dict(pkg.payload)
    payload["selected_quote_id"] = quote_id
    payload["status"] = "PRESENTED"
    pkg.payload = payload
    session.add(pkg)
    await session.commit()

    await fire_quote_comparison_result(
        session, ctx,
        submission_id=item.submission_id, named_insured=payload.get("named_insured"),
        selected_quote=quote,
    )
    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what=f"selected quote={quote_id}",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id, "quote_id": quote_id},
        ),
    )
    return ReviewItemOut(
        id=acted.id, submission_id=acted.submission_id, status=acted.status.value,
        payload=ComparisonPayload(**payload),
    )


async def _log_only_action(
    item_id: str, ctx: Ctx, session: AsyncSession, *, new_status: str, what: str
) -> ReviewItemOut:
    """FR-23's other two logged actions ("requesting revised terms", "no
    action, quote will lapse") — payload-status-only, no ReviewAction/
    ReviewStatus transition (neither frozen enum has a matching value; same
    pattern as agent_communication's discard/compliance-clear)."""
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    if pkg is None or pkg.payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no comparison payload for this item")

    payload = dict(pkg.payload)
    payload["status"] = new_status
    pkg.payload = payload
    session.add(pkg)
    await session.commit()

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what=what,
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=ComparisonPayload(**payload),
    )


@router.post("/{item_id}/request-revised-terms")
async def request_revised_terms(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    return await _log_only_action(
        item_id, ctx, session, new_status="REVISION_REQUESTED", what="requested revised terms"
    )


@router.post("/{item_id}/mark-lapsed")
async def mark_lapsed(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    return await _log_only_action(
        item_id, ctx, session, new_status="LAPSED", what="marked no action — quote will lapse"
    )
