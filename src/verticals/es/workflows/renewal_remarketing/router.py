"""Routes under ``/api/es/renewal-remarketing`` (docs/WORKFLOW_TEMPLATE.md
step 5). Registered by ``verticals/es/router.py`` — the one shared-file
line the E&S dev touches to mount a workflow.

RR-05's Market Matching re-invocation lives here, at
``/{item_id}/initiate-remarket`` — a SEPARATE, broker-approval-gated
action, not automatic inside ``/run`` (per the PRD's own §4 step 6b: "broker
approves initiating a remarket, which re-invokes Market Matching").

**Formerly a known v1 limitation, now fixed for the live path:**
``initiate-remarket`` used to always re-invoke ``MarketMatchingPipeline.run()``
against the Workflow_10 FIXTURE for the same named insured, even when the
review item itself was real. Now it accepts an optional real
``message_id`` (picked from ``/live-inbox``, same discovery search as every
other live-inbox picker this vertical uses) — when supplied,
``MarketMatchingPipeline.run()`` runs against that REAL message
(``build_connector_service`` already switches to the live connector
automatically), a genuine "current profile" re-extraction, not stale
bind-time fixture data. Omitting ``message_id`` keeps the exact original
fixture-fallback behavior, unchanged, for backward compatibility.

RR-06's comparison stage (incumbent renewal offer vs. a remarketed
alternative) is also now live: ``/{item_id}/run-live-comparison`` parses a
real incumbent-offer email (``incumbent_offer_parser.py``) against a real,
already-selected Quote Comparison quote for the same account
(``live_ingestion.find_live_alternative_quote``) — see that module for why
reusing Quote Comparison's own real output, rather than inventing a second
live-ingestion path for "the alternative," is the right design.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, RawBundle, RawDocument, WorkflowInput
from core.common.enums import ReviewAction, Vertical
from core.db import get_session
from core.extraction import DefaultExtractionService
from core.ingestion.connectors import ConnectorNotConnectedError, build_connector_service
from core.llm import build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.review_queue import AuthorityError, DefaultReviewQueueService
from core.rules_engine import DefaultRulesEngine
from core.tenancy.dependencies import get_ctx
from verticals.es.workflows.market_matching.service import MarketMatchingPipeline
from verticals.es.workflows.renewal_remarketing.incumbent_offer_parser import (
    parse_incumbent_renewal_offer,
)
from verticals.es.workflows.renewal_remarketing.live_ingestion import (
    discover_live_binds,
    discover_live_messages,
    find_live_alternative_quote,
    save_live_incumbent_offer,
)
from verticals.es.workflows.renewal_remarketing.schema import RemarketDecisionPayload
from verticals.es.workflows.renewal_remarketing.service import (
    DEFAULT_WORKFLOW_N,
    WORKFLOW_NAME,
    RenewalRemarketingPipeline,
)

router = APIRouter(prefix="/renewal-remarketing", tags=["es:renewal-remarketing"])

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

_MARKET_MATCHING_WORKFLOW_N = 10


def _pipeline() -> RenewalRemarketingPipeline:
    return RenewalRemarketingPipeline(llm=build_llm_service(), workflow_n=DEFAULT_WORKFLOW_N)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.strip().lower()).strip()


async def _resolve_workflow10_submission_ref(ctx: Ctx, named_insured: str) -> str | None:
    """Finds which Workflow_10 submission fixture matches this named
    insured, so RR-05's re-invocation targets the right one — a small,
    native re-implementation of package_assembly's ``submission_resolver.py``
    pattern (no cross-workflow import, same precedent as every prior
    workflow's native parsing/reconciliation logic)."""
    from fixtures import load_workflow

    target = _normalize(named_insured)
    extraction = DefaultExtractionService()
    loaded = load_workflow(
        _MARKET_MATCHING_WORKFLOW_N, tenant_id=ctx.tenant_id, vertical=Vertical.ES
    )
    for ls in loaded:
        raw = RawBundle(
            submission_id=ls.submission.external_ref,
            documents=[
                RawDocument(kind=d.kind, filename=d.filename, content=d.content or "", uri=d.uri)
                for d in ls.documents
            ],
        )
        model = await extraction.extract(ctx, raw)
        candidate = next((f.value for f in model.fields if f.name == "acord.named_insured"), None)
        if candidate is not None and _normalize(str(candidate)) == target:
            return ls.submission.external_ref
    return None


class RunRequest(BaseModel):
    scenario_ref: str


class RunLiveRequest(BaseModel):
    bind_id: str


class LiveBindOut(BaseModel):
    bind_id: str
    named_insured: str | None = None
    carrier_name: str | None = None


class LiveInboxMessageOut(BaseModel):
    id: str
    subject: str


class LiveAlternativeQuoteOut(BaseModel):
    quote_comparison_item_id: str
    carrier_name: str
    premium: float | None = None
    limits: str | None = None


class InitiateRemarketRequest(BaseModel):
    # Real Gmail message id, picked from /live-inbox — omit to keep the
    # original Workflow_10 fixture-matching behavior unchanged.
    message_id: str | None = None


class RunLiveComparisonRequest(BaseModel):
    message_id: str  # the incumbent's real renewal-offer email
    quote_comparison_item_id: str  # a real, already-selected Quote Comparison item


class ReviewItemOut(BaseModel):
    id: str
    submission_id: str | None
    status: str
    payload: RemarketDecisionPayload | None = None


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
            status.HTTP_404_NOT_FOUND, f"no renewal-remarketing review item '{item_id}'"
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
async def run_renewal_remarketing(
    body: RunRequest, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    pipeline = _pipeline()
    output = await pipeline.run(ctx, WorkflowInput(source_ref=body.scenario_ref))

    review_queue = DefaultReviewQueueService()
    item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=RemarketDecisionPayload(**output.payload),
    )


@router.get("/live-binds")
async def list_live_binds(ctx: CtxDep, session: SessionDep) -> list[LiveBindOut]:
    """Additive: every real Binder Issuance bind for this tenant, for the
    "Check live renewal" picker — see live_ingestion.py."""
    binds = await discover_live_binds(session, ctx)
    return [LiveBindOut(**b) for b in binds]


@router.get("/live-inbox")
async def list_live_inbox(
    item_id: str, ctx: CtxDep, session: SessionDep
) -> list[LiveInboxMessageOut]:
    """Real Gmail messages for this item's real named insured — reused for
    both re-shop-candidate discovery (feeding ``initiate-remarket``'s
    optional ``message_id``) and incumbent-offer discovery (feeding
    ``run-live-comparison``); same underlying search either way, the broker
    picks based on subject. Additive; requires Gmail connected +
    CONNECTORS_MODE=live."""
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    named_insured = (pkg.payload or {}).get("named_insured") if pkg else None
    try:
        messages = await discover_live_messages(session, ctx, named_insured)
    except ConnectorNotConnectedError as exc:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            f"Connect Gmail in Settings first ({exc.provider} not connected)",
        ) from exc
    return [LiveInboxMessageOut(**m) for m in messages]


@router.get("/live-alternative-quotes")
async def list_live_alternative_quotes(
    item_id: str, ctx: CtxDep, session: SessionDep
) -> list[LiveAlternativeQuoteOut]:
    """Real, already-selected Quote Comparison items for this item's real
    named insured — the "alternative" side of RR-06's comparison, for the
    ``run-live-comparison`` picker. See ``live_ingestion.
    find_live_alternative_quote``'s docstring for why this reuses Quote
    Comparison's own real output rather than a second live-ingestion path."""
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    named_insured = (pkg.payload or {}).get("named_insured") if pkg else None
    if not named_insured:
        return []
    # Joined via ReviewItemRow (not a bare OutputPackageRow scan) so the id
    # returned is the one /api/es/quote-comparison/{item_id} actually
    # expects — the STABLE review item id, not the internal output-package
    # row id, same distinction the bind_id fix above is about.
    qc_items = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.workflow) == "quote_comparison",
            )
        )
    ).scalars().all()
    out = []
    for qc_item in qc_items:
        if not qc_item.output_package_id:
            continue
        row = (
            await session.execute(
                select(OutputPackageRow).where(
                    col(OutputPackageRow.id) == qc_item.output_package_id
                )
            )
        ).scalar_one_or_none()
        p = row.payload if row and row.payload else {}
        if p.get("named_insured") != named_insured or not p.get("selected_quote_id"):
            continue
        quote = next(
            (q for q in p.get("quotes", []) if q.get("quote_id") == p["selected_quote_id"]), None
        )
        if quote is None:
            continue
        out.append(LiveAlternativeQuoteOut(
            quote_comparison_item_id=qc_item.id, carrier_name=quote.get("carrier_name", ""),
            premium=quote.get("premium"), limits=quote.get("limits"),
        ))
    return out


@router.post("/run-live", status_code=status.HTTP_201_CREATED)
async def run_renewal_remarketing_live(
    body: RunLiveRequest, ctx: CtxDep, session: SessionDep
) -> ReviewItemOut:
    """Additive alongside ``/run`` above: a real trigger-stage review built
    from an ACTUAL Binder Issuance bind + real Endorsement Processing
    history for it, instead of a Workflow_16 fixture. See
    ``live_ingestion.py``."""
    pipeline = _pipeline()
    output = await pipeline.run_live(ctx, session, body.bind_id)

    review_queue = DefaultReviewQueueService()
    item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=RemarketDecisionPayload(**output.payload),
    )


@router.get("")
async def list_renewal_remarketing(ctx: CtxDep, session: SessionDep) -> list[ReviewItemOut]:
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
async def get_renewal_remarketing(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=RemarketDecisionPayload(**pkg.payload) if pkg and pkg.payload else None,
    )


@router.post("/{item_id}/initiate-remarket")
async def initiate_remarket(
    item_id: str, ctx: CtxDep, session: SessionDep, body: InitiateRemarketRequest | None = None,
) -> ReviewItemOut:
    """FR-16: "Approve light check" / "Approve full remarket" — both use
    this same action; the level distinction is already in the trigger
    decision, not the action itself. Pass a real ``message_id`` (from
    ``/live-inbox``) to re-invoke Market Matching against a genuine current
    document instead of the Workflow_10 fixture fallback — see module
    docstring."""
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    if pkg is None or pkg.payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no remarket payload for this item")

    payload = dict(pkg.payload)
    level = payload.get("trigger_decision", {}).get("level")
    if level == "NO_REMARKET":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "NO_REMARKET decisions don't initiate a remarket — use accept-incumbent instead",
        )

    message_id = body.message_id if body else None
    if message_id:
        submission_ref: str | None = message_id
    else:
        named_insured = payload.get("named_insured") or ""
        submission_ref = await _resolve_workflow10_submission_ref(ctx, named_insured)
    market_matching_item_id = None
    if submission_ref:
        mm_pipeline = MarketMatchingPipeline(
            session=session,
            connector=build_connector_service(
                workflow_n=_MARKET_MATCHING_WORKFLOW_N, session=session
            ),
            extraction=DefaultExtractionService(),
            rules_engine=DefaultRulesEngine(),
            llm=build_llm_service(),
            workflow_n=_MARKET_MATCHING_WORKFLOW_N,
        )
        mm_output = await mm_pipeline.run(ctx, WorkflowInput(source_ref=submission_ref))
        mm_item = await DefaultReviewQueueService().enqueue(
            session, ctx, mm_output, "market_matching"
        )
        market_matching_item_id = mm_item.id

    remarket_execution = dict(payload.get("remarket_execution") or {})
    remarket_execution["initiated"] = True
    remarket_execution["market_matching_output_id"] = market_matching_item_id
    payload["remarket_execution"] = remarket_execution
    pkg.payload = payload
    session.add(pkg)
    await session.commit()

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what=f"initiated remarket (level={level})",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id, "market_matching_item_id": market_matching_item_id},
        ),
    )
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=RemarketDecisionPayload(**payload),
    )


@router.post("/{item_id}/run-live-comparison", status_code=status.HTTP_201_CREATED)
async def run_renewal_remarketing_live_comparison(
    item_id: str, body: RunLiveComparisonRequest, ctx: CtxDep, session: SessionDep,
) -> ReviewItemOut:
    """RR-06, live: a real incumbent-offer email against a real, already-
    selected Quote Comparison quote — instead of Workflow_16's Scenario 05
    fixture. ``item_id`` is the TRIGGER-stage review (read-only here, never
    mutated) — a comparison only makes sense once a remarket was actually
    approved, matching the fixture's own precedent of Scenario 05 being a
    wholly separate scenario/review item from Scenario 02, not an in-place
    update of it."""
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    if pkg is None or pkg.payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no remarket payload for this item")
    trigger_payload = pkg.payload
    if not (trigger_payload.get("remarket_execution") or {}).get("initiated"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no remarket has been initiated for this review yet — approve one first",
        )

    try:
        offer_text = await save_live_incumbent_offer(session, ctx, item_id, body.message_id)
    except ConnectorNotConnectedError as exc:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            f"Connect Gmail in Settings first ({exc.provider} not connected)",
        ) from exc
    parsed_offer = parse_incumbent_renewal_offer(offer_text)

    alternative = await find_live_alternative_quote(
        session, ctx, trigger_payload.get("named_insured")
    )
    if alternative is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no selected Quote Comparison quote found for this named insured yet",
        )

    context = {
        "bind_id": trigger_payload.get("bind_id"),
        "named_insured": trigger_payload.get("named_insured"),
        "incumbent_carrier_id": trigger_payload.get("incumbent_carrier_id"),
        "incumbent_carrier_name": trigger_payload.get("incumbent_carrier_name", ""),
        "incumbent_renewal_offer": {
            "premium": parsed_offer.premium,
            "limits": parsed_offer.limits,
            "deductible": parsed_offer.deductible,
        },
        "alternative_quote_received": alternative,
    }
    pipeline = _pipeline()
    output = await pipeline.run_live_comparison(ctx, context)

    review_queue = DefaultReviewQueueService()
    new_item = await review_queue.enqueue(session, ctx, output, WORKFLOW_NAME)
    return ReviewItemOut(
        id=new_item.id, submission_id=new_item.submission_id, status=new_item.status.value,
        payload=RemarketDecisionPayload(**output.payload),
    )


@router.post("/{item_id}/accept-incumbent")
async def accept_incumbent(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """FR-16: "Accept incumbent terms (NO_REMARKET)" — workflow-owned
    since ``ReviewAction`` has no matching value and this records
    ``final_decision``, which none of the frozen actions carry."""
    item = await _item_or_404(item_id, ctx, session)
    pkg = await _pkg_row_for(session, item)
    if pkg is None or pkg.payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no remarket payload for this item")

    payload = dict(pkg.payload)
    final_decision = dict(payload.get("final_decision") or {})
    final_decision["outcome"] = "renewed_incumbent"
    final_decision["decided_by"] = ctx.user_id
    final_decision["timestamp"] = datetime.now(UTC).isoformat()
    payload["final_decision"] = final_decision
    pkg.payload = payload
    session.add(pkg)
    await session.commit()

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what="accepted incumbent renewal terms",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    return ReviewItemOut(
        id=item.id, submission_id=item.submission_id, status=item.status.value,
        payload=RemarketDecisionPayload(**payload),
    )


@router.post("/{item_id}/escalate")
async def escalate(item_id: str, ctx: CtxDep, session: SessionDep) -> ReviewItemOut:
    """FR-16: "Escalate urgent remarket" — reuses the existing frozen
    ``ReviewAction.ESCALATE``, which already fits."""
    try:
        item = await DefaultReviewQueueService().act(session, ctx, item_id, ReviewAction.ESCALATE)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    await DefaultAuditService().record(
        session, ctx,
        AuditEntry(
            actor="human", who=ctx.user_id, what="escalated urgent remarket",
            workflow=WORKFLOW_NAME, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"item_id": item_id},
        ),
    )
    return ReviewItemOut(id=item.id, submission_id=item.submission_id, status=item.status.value)
