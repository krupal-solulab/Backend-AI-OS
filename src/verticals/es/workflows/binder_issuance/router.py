"""Routes under ``/api/es/binder-issuance`` (docs/WORKFLOW_TEMPLATE.md step
5). Registered by ``verticals/es/router.py`` — the one shared-file line the
E&S dev touches to mount a workflow.

Unlike Quote Comparison (where the broker's explicit ``/select`` is the
downstream-trigger point), Placement Confirmation / Policy Documents
Delivered fire AUTOMATICALLY right here in ``/run`` (and again from the
``resolve-*-discrepancy`` endpoints, if a discrepancy blocked them the first
time) — same pattern as Market Matching/Package Assembly's hooks, because
BI-06's gate is "a verified-clean reconciliation," not "the broker chose,"
and that gate is already evaluated by the service before this router acts.
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
from core.llm import build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.review_queue import AuthorityError, DefaultReviewQueueService
from core.tenancy.dependencies import get_ctx
from verticals.es.agent_communication_hooks import fire_binder_issuance_result
from verticals.es.workflows.binder_issuance.coordination_engine import recompute_live_state
from verticals.es.workflows.binder_issuance.live_ingestion import (
    build_broker_bind_instruction_from_quote,
)
from verticals.es.workflows.binder_issuance.schema import BindCoordinationPayload
from verticals.es.workflows.binder_issuance.service import (
    DEFAULT_WORKFLOW_N,
    WORKFLOW_NAME,
    BinderIssuancePipeline,
)

router = APIRouter(prefix="/binder-issuance", tags=["es:binder-issuance"])

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

_RESOLUTIONS = {"accept_carrier_version", "flag_carrier_error"}
_POLICY_RESOLUTIONS = {"accept_issued_version", "flag_carrier_error"}


def _pipeline() -> BinderIssuancePipeline:
    return BinderIssuancePipeline(llm=build_llm_service(), workflow_n=DEFAULT_WORKFLOW_N)


class RunRequest(BaseModel):
    scenario_ref: str
    as_of: str | None = None  # ISO date override — fixture/test determinism only


class RunFromQuoteRequest(BaseModel):
    quote_comparison_item_id: str


class ResolveDiscrepancyRequest(BaseModel):
    resolution: str  # accept_carrier_version | accept_issued_version | flag_carrier_error


class ReviewItemOut(BaseModel):
    id: str
    submission_id: str | None
    status: str
    payload: BindCoordinationPayload | None = None


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
            status.HTTP_404_NOT_FOUND, f"no binder-issuance review item '{item_id}'"
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
async def run_binder_issuance(body: RunRequest, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    pipeline = _pipeline()
    inp = WorkflowInput(
        source_ref=body.scenario_ref, params={"as_of": body.as_of} if body.as_of else {}
    )
    output = await pipeline.run(ctx, inp)

    review_queue = DefaultReviewQueueService()
    item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)

    await fire_binder_issuance_result(
        session, ctx, submission_id=item.submission_id, payload=output.payload
    )
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=BindCoordinationPayload(**output.payload),
    )


@router.post("/run-from-quote-comparison", status_code=status.HTTP_201_CREATED)
async def run_binder_issuance_from_quote(
    body: RunFromQuoteRequest, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    """Additive alongside ``/run`` above: starts a real pre-bind pass from
    an actual, already-selected Quote Comparison item's real terms, instead
    of a Workflow_14 fixture. See ``live_ingestion.py``."""
    instruction = await build_broker_bind_instruction_from_quote(session, ctx, body.quote_comparison_item_id)

    pipeline = _pipeline()
    output = await pipeline.run_live(ctx, instruction)

    review_queue = DefaultReviewQueueService()
    item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)

    await fire_binder_issuance_result(
        session, ctx, submission_id=item.submission_id, payload=output.payload
    )
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=BindCoordinationPayload(**output.payload),
    )


@router.get("")
async def list_binder_issuance(ctx: CtxDep, session: SessionDep) -> list[ReviewItemOut]:
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
async def get_binder_issuance(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """BI-04/BI-07: recomputes overdue/reminder status against TODAY on
    every read — same read-time-projection pattern as Quote Comparison's
    QC-07 (no scheduled job yet, per the approved plan)."""
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    payload: dict[str, Any] | None = None
    if pkg and pkg.payload:
        payload = recompute_live_state(pkg.payload, datetime.now(UTC).date())
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=BindCoordinationPayload(**payload) if payload else None,
    )


async def _resolve_discrepancy(
    item_id: str, ctx: Ctx, session: AsyncSession, body: ResolveDiscrepancyRequest,
    *, target: str, valid_resolutions: set[str], trigger_key: str,
) -> ReviewItemOut:
    if body.resolution not in valid_resolutions:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"resolution must be one of {sorted(valid_resolutions)}",
        )
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    if pkg is None or pkg.payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no coordination payload for this item")

    payload = dict(pkg.payload)
    section_key = (
        "carrier_confirmation" if target == "confirmation" else "issued_policy_reconciliation"
    )
    section = dict(payload.get(section_key) or {})
    if section.get("reconciliation_status") == "DISCREPANCY_FLAGGED" or (
        section.get("status") == "POLICY_DISCREPANCY_FLAGGED"
    ):
        if target == "confirmation":
            section["reconciliation_status"] = "BROKER_RESOLVED"
        else:
            section["status"] = "BROKER_RESOLVED"
    payload[section_key] = section

    triggers = dict(payload.get("downstream_triggers_fired") or {})
    already_fired = triggers.get(trigger_key, False)
    triggers[trigger_key] = True
    payload["downstream_triggers_fired"] = triggers

    pkg.payload = payload
    session.add(pkg)
    await session.commit()

    if not already_fired:
        # Fire ONLY the trigger just resolved — the stored payload's OTHER
        # trigger flag may already be True (e.g. placement_confirmation on a
        # bind_record-stage snapshot, per its own "assumed clean" v1
        # simplification), and re-passing the whole dict would re-fire it
        # a second time.
        single_trigger_payload = {**payload, "downstream_triggers_fired": {trigger_key: True}}
        await fire_binder_issuance_result(
            session, ctx, submission_id=item.submission_id, payload=single_trigger_payload
        )

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id,
            what=f"resolved {target} discrepancy: {body.resolution}",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id, "resolution": body.resolution},
        ),
    )
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=BindCoordinationPayload(**payload),
    )


@router.post("/{item_id}/resolve-confirmation-discrepancy")
async def resolve_confirmation_discrepancy(
    item_id: str, body: ResolveDiscrepancyRequest, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    """FR-8/FR-23: broker explicitly resolves a BI-03 discrepancy (Accept
    carrier's version / Flag as carrier error) — required before Placement
    Confirmation can fire (BI-06). Workflow-owned: ``ReviewAction`` has no
    matching frozen value, same pattern as Agent Communication's
    compliance-clear and Quote Comparison's select/request-revised-terms."""
    return await _resolve_discrepancy(
        item_id, ctx, session, body,
        target="confirmation", valid_resolutions=_RESOLUTIONS,
        trigger_key="placement_confirmation",
    )


@router.post("/{item_id}/resolve-policy-discrepancy")
async def resolve_policy_discrepancy(
    item_id: str, body: ResolveDiscrepancyRequest, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    """FR-14/FR-23: broker explicitly resolves a BI-05 discrepancy — required
    before Policy Documents Delivered can fire (BI-06)."""
    return await _resolve_discrepancy(
        item_id, ctx, session, body,
        target="issued_policy", valid_resolutions=_POLICY_RESOLUTIONS,
        trigger_key="policy_documents_delivered",
    )


@router.post("/{item_id}/escalate")
async def escalate(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """FR-23's third resolution path — reuses the existing frozen
    ``ReviewAction.ESCALATE`` (anyone may escalate), unlike the other two
    resolution actions which don't map onto any existing action."""
    try:
        item = await DefaultReviewQueueService().act(session, ctx, item_id, ReviewAction.ESCALATE)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what="escalated to principal",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    return ReviewItemOut(id=item.id, submission_id=item.submission_id, status=item.status.value)
