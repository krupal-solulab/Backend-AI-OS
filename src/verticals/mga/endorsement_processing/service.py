"""Endorsement Processing pipeline — ingest(dataset fixture) → classify (MEP-01) →
premium impact via the real RatingEngine (MEP-03) → delegated authority recheck (MEP-02)
→ record/referral drafting (MEP-04/06) → PAS write-back (MEP-05) → review → audit.
Orchestration layer over already-built engines, per the PRD — no reimplemented rating
logic, no new appetite model. No auto-processing, no auto-referral outcome — human
approves every processed change and every referral (FR unconditional across every
workflow in this project).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Citation, Ctx, Draft, ExtractedValue
from core.common.dtos import Decision as DecisionDTO
from core.common.dtos import OutputPackage as OutputPackageDTO
from core.common.enums import DecisionOutcome, ReviewAction
from core.llm import build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.models import Submission
from core.review_queue import DefaultReviewQueueService
from verticals.mga.endorsement_processing.engine import (
    BLOCKED_STALE_RATE,
    CONFIRMED_EXCLUSION_DECLINED,
    EXCEEDS_CEILING_REFERRAL_REQUIRED,
    WITHIN_AUTHORITY,
    EndorsementDecision,
    EndorsementEngine,
)
from verticals.mga.endorsement_processing.fixtures import load_scenario
from verticals.mga.endorsement_processing.schema import (
    ActivityEntry,
    AuthorityCheckOut,
    CarrierReferralOut,
    EndAppetite,
    EndDiffRow,
    EndorsementDetail,
    EndorsementRow,
    WriteBackRecordOut,
)
from verticals.mga.models import MgaEndorsementResult

WORKFLOW = "endorsement-processing"

_STATUS_LABEL = {
    WITHIN_AUTHORITY: "READY_TO_PROCESS",
    EXCEEDS_CEILING_REFERRAL_REQUIRED: "BLOCKED_REFERRAL",
    CONFIRMED_EXCLUSION_DECLINED: "BLOCKED_DECLINED",
    BLOCKED_STALE_RATE: "BLOCKED_STALE_RATE",
}
_ACTIONS = {
    "approve": ReviewAction.APPROVE,
    "send": ReviewAction.SEND,       # human-triggered carrier referral send
    "escalate": ReviewAction.ESCALATE,
}


class EndorsementService:
    def __init__(self) -> None:
        self.engine = EndorsementEngine()
        self.llm = build_llm_service()
        self.review_queue = DefaultReviewQueueService()
        self.audit = DefaultAuditService()

    async def process(
        self, session: AsyncSession, ctx: Ctx, scenario: str,
        request_override: dict[str, Any] | None = None,
    ) -> EndorsementDetail:
        request = request_override or load_scenario(scenario)
        if request is None:
            raise KeyError(f"no endorsement fixture '{scenario}' for Workflow-04")

        sub = Submission(tenant_id=ctx.tenant_id, vertical=ctx.vertical,
                         external_ref=scenario, subject=request.get("policy_number", scenario),
                         status="endorsement")
        session.add(sub)
        await session.flush()

        decision = self.engine.decide(request)
        narrative, citations = await self._narrative(ctx, request, decision)
        detail = self._build_detail(sub.id, scenario, request, decision, narrative, citations)
        row = self._build_row(sub.id, request, detail)

        write_back_logged = decision.outcome == WITHIN_AUTHORITY
        session.add(MgaEndorsementResult(
            tenant_id=ctx.tenant_id, submission_id=sub.id, classification=decision.classification,
            outcome=decision.outcome, premium_impact=decision.premium_impact,
            resulting_total_premium=decision.resulting_total_premium,
            excluded_class_matched=decision.excluded_class_matched,
            carrier_referral_drafted=decision.outcome == EXCEEDS_CEILING_REFERRAL_REQUIRED,
            write_back_logged=write_back_logged, bordereau_schema_validated=write_back_logged))

        out_dto = OutputPackageDTO(
            submission_id=sub.id,
            decision=DecisionDTO(
                outcome=(DecisionOutcome.PROCEED if decision.outcome == WITHIN_AUTHORITY
                        else DecisionOutcome.REQUEST_INFO if decision.outcome == BLOCKED_STALE_RATE
                        else DecisionOutcome.DECLINE),
                score=None, rationale=decision.rationale),
            draft=Draft(text=narrative, citations=citations),
            flags=[decision.outcome], missing_info=[],
            payload={"detail": detail.model_dump(by_alias=True), "row": row.model_dump()})
        await self.review_queue.enqueue(session, ctx, out_dto, WORKFLOW)

        await self.audit.record(session, ctx, AuditEntry(
            actor="ai", who="system", what=f"Endorsement classified: {decision.outcome}",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"scenario": scenario, "outcome": decision.outcome}))
        await session.commit()
        return detail

    async def _narrative(
        self, ctx: Ctx, request: dict[str, Any], decision: EndorsementDecision
    ) -> tuple[str, list[Citation]]:
        if decision.outcome == CONFIRMED_EXCLUSION_DECLINED:
            return (f"Declined outright: {decision.rationale} Narrative suppressed pending "
                    "broker communication about a possible separate placement."), []
        facts = [
            ExtractedValue(name="requested_change.type",
                           value=request.get("requested_change", {}).get("type")),
            ExtractedValue(name="requested_change.detail",
                           value=request.get("requested_change", {}).get("detail")),
        ]
        draft = await self.llm.draft(
            ctx, "Write a concise 2-3 sentence summary of this mid-term endorsement request "
            "and its outcome, using only these facts.", facts, tier="fast")
        return draft.text, draft.citations

    def _build_detail(
        self, sub_id: str, scenario: str, request: dict[str, Any], decision: EndorsementDecision,
        narrative: str, citations: list[Citation],
    ) -> EndorsementDetail:
        change = request.get("requested_change", {})
        now = datetime.now(UTC).isoformat()
        premium_impact = decision.premium_impact
        within_authority = decision.outcome == WITHIN_AUTHORITY
        exceeds_ceiling = decision.outcome == EXCEEDS_CEILING_REFERRAL_REQUIRED
        premium_bearing = bool(premium_impact) or exceeds_ceiling
        delta_str = (f"+${premium_impact:,.0f}" if premium_impact else
                    "TBD — pending carrier referral" if exceeds_ceiling else "No change")

        write_back = WriteBackRecordOut(
            logged=within_authority, bordereauSchemaValidated=within_authority,
            transactionType="ENDORSEMENT" if within_authority else None,
            policyNumber=request.get("policy_number") if within_authority else None,
            effectiveDate=now if within_authority else None,
            premiumDelta=premium_impact if within_authority else None)

        return EndorsementDetail(
            classification=decision.classification,
            premiumBearing=premium_bearing,
            premiumDelta=delta_str,
            rationale=decision.rationale,
            diff=[EndDiffRow(**d) for d in decision.diff],
            appetite=[EndAppetite(rule=a["rule"], passed=a["pass"], hard=a["hard"],
                                  detail=a["detail"]) for a in decision.appetite],
            hardRulePassed=decision.outcome != CONFIRMED_EXCLUSION_DECLINED,
            schedule=decision.schedule,
            endorsementId=f"MEP-{sub_id[-6:]}",
            policyNumber=str(request.get("policy_number", "")),
            namedInsured=str(request.get("named_insured", "")),
            requestedChangeType=str(change.get("type", "")),
            requestedChangeDetail=str(change.get("detail", "")),
            status=_STATUS_LABEL[decision.outcome],
            authorityCheck=AuthorityCheckOut(
                outcome=decision.outcome,
                delegatedCeiling=f"${decision.delegated_ceiling:,.0f}",
                resultingTotalPremium=(f"${decision.resulting_total_premium:,.0f}"
                                       if decision.resulting_total_premium is not None else None),
                excludedClassMatched=decision.excluded_class_matched),
            carrierReferral=CarrierReferralOut(
                drafted=decision.outcome == EXCEEDS_CEILING_REFERRAL_REQUIRED,
                carrierResponse=None, draftText=decision.carrier_referral_text),
            writeBackRecord=write_back,
            activity=[ActivityEntry(at=now, who="system (AI)",
                                    what=f"Endorsement classified -> {decision.outcome}",
                                    ctx=scenario)],
        )

    @staticmethod
    def _build_row(
        sub_id: str, request: dict[str, Any], detail: EndorsementDetail,
    ) -> EndorsementRow:
        # id is the real submission id (same convention as Triage/Renewal rows) — the FE
        # uses it directly for GET/act calls; endorsementId stays a cosmetic display label.
        return EndorsementRow(
            id=sub_id, policy=str(request.get("policy_number", "")),
            insured=str(request.get("named_insured", "")),
            type=str(request.get("requested_change", {}).get("type", "")),
            requested="just now", impact=detail.premiumDelta, status="pending")

    # ── list / detail / act ──
    async def list_rows(self, session: AsyncSession, ctx: Ctx) -> list[EndorsementRow]:
        stmt = (select(ReviewItemRow, OutputPackageRow)
                .join(OutputPackageRow,
                      col(ReviewItemRow.output_package_id) == col(OutputPackageRow.id))
                .where(col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                       col(ReviewItemRow.workflow) == WORKFLOW))
        rows: list[EndorsementRow] = []
        for item, pkg in (await session.execute(stmt)).all():
            payload = pkg.payload or {}
            if "row" not in payload:
                continue
            row = EndorsementRow(**payload["row"])
            row.status = item.status.value if hasattr(item.status, "value") else str(item.status)
            rows.append(row)
        return rows

    async def get_detail(
        self, session: AsyncSession, ctx: Ctx, submission_id: str
    ) -> EndorsementDetail | None:
        pkg = (await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.submission_id) == submission_id,
                col(OutputPackageRow.workflow) == WORKFLOW))
        ).scalars().first()
        if pkg is None or not pkg.payload:
            return None
        return EndorsementDetail.model_validate(pkg.payload["detail"])

    async def act(
        self, session: AsyncSession, ctx: Ctx, submission_id: str, action: str,
    ) -> dict[str, str]:
        review_action = _ACTIONS.get(action)
        if review_action is None:
            raise ValueError(f"unknown action '{action}'; allowed: {sorted(_ACTIONS)}")
        item = (await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.submission_id) == submission_id,
                col(ReviewItemRow.workflow) == WORKFLOW))
        ).scalars().first()
        if item is None:
            raise KeyError(
                f"no endorsement-processing review item for submission '{submission_id}'")
        result = await self.review_queue.act(session, ctx, item.id, review_action)
        await self.audit.record(session, ctx, AuditEntry(
            actor="human", who=ctx.user_id, what=f"{action} (role={ctx.role.value})",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"submission": submission_id, "action": action}))
        await session.commit()
        return {"id": result.id, "status": result.status.value}
