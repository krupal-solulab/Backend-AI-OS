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

from typing import Annotated, Literal

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
from verticals.es.workflows.diligent_search.live_ingestion import discover_live_stub_submissions
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


class LiveStubSubmissionOut(BaseModel):
    item_id: str
    submission_id: str | None


class LiveDeclinationInput(BaseModel):
    carrier: str
    date: str | None = None
    written_evidence: bool = False


class LiveStateInput(BaseModel):
    state: str
    # Broker-supplied determination — never inferred. "pending" means "not
    # yet checked for this state," which DS-01 already handles honestly via
    # requirement=None (PENDING_DETERMINATION), no new logic needed.
    status: Literal["exempt", "required", "pending"]
    export_list_note: str | None = None
    admitted_declinations_required: int | None = None
    declinations: list[LiveDeclinationInput] = []


class RunLiveRequest(BaseModel):
    submission_id: str | None = None
    named_insured: str | None = None
    states: list[LiveStateInput]


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


async def _pkg_row_for(session: AsyncSession, item: ReviewItemRow) -> OutputPackageRow | None:
    if not item.output_package_id:
        return None
    return (
        await session.execute(
            select(OutputPackageRow).where(col(OutputPackageRow.id) == item.output_package_id)
        )
    ).scalar_one_or_none()


async def _payload_for(
    session: AsyncSession, item: ReviewItemRow
) -> ComplianceRecordPayload | None:
    pkg = await _pkg_row_for(session, item)
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


@router.get("/live-submissions")
async def list_live_submissions(ctx: CtxDep, session: SessionDep) -> list[LiveStubSubmissionOut]:
    """Real submissions MM-07 has already flagged (a real, linked stub
    review item exists) but that no broker has completed a real search
    for yet — see live_ingestion.py's module docstring for why this
    discovers rather than auto-determines."""
    stubs = await discover_live_stub_submissions(session, ctx)
    return [LiveStubSubmissionOut(**s) for s in stubs]


@router.post("/{item_id}/run-live")
async def run_diligent_search_live(
    item_id: str, body: RunLiveRequest, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    """Additive alongside ``/run`` above: a real per-state determination
    from broker/compliance-supplied facts and declination records for a
    real, MM-07-seeded submission — updates this SAME review item in
    place (no new item created), same direct-mutation technique as
    Binder & Issuance's ``attach_live_confirmation``."""
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    if pkg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no compliance record payload for this item")

    states: list[dict] = []
    for s in body.states:
        if s.status == "pending":
            requirement = None
            declinations = None
        elif s.status == "exempt":
            requirement = {"export_list_class": True, "export_list_note": s.export_list_note}
            declinations = None
        elif s.admitted_declinations_required is None:
            # "Required" but the broker hasn't entered how many declinations
            # are actually needed — treating that as 0 would let ANY (even
            # zero) declinations trivially satisfy the sufficiency check,
            # the exact silent-false-pass this workflow's zero-tolerance
            # gate exists to prevent. Honest instead: requirement=None
            # routes through DS-01's own null-safety to PENDING_DETERMINATION
            # ("not yet checked"), never a fabricated count.
            requirement = None
            declinations = None
        else:  # "required", with a real broker-supplied count
            requirement = {
                "export_list_class": False,
                "admitted_declinations_required": s.admitted_declinations_required,
            }
            declinations = [d.model_dump() for d in s.declinations]
        states.append({"state": s.state, "requirement": requirement, "declinations": declinations})

    pipeline = _pipeline()
    output = await pipeline.run_live(
        ctx,
        {
            "submission_id": body.submission_id or item.submission_id,
            "named_insured": body.named_insured,
            "states": states,
        },
    )

    pkg.payload = output.payload
    session.add(pkg)
    await session.commit()
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=ComplianceRecordPayload(**output.payload),
    )


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
