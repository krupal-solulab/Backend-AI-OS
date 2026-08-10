"""Bind Order & Issuance pipeline — ingest(dataset fixture) → worksheet fidelity gate
(MBI-01) → pre-bind subjectivity gate (MBI-02) → final authority reconfirmation against
current information (MBI-03) → PAS write-back (MBI-04) → issuance reconciliation
(MBI-05) → downstream trigger gating (MBI-06) → post-bind obligation tracking (MBI-07)
→ review → audit. Orchestration layer over Quoting & Rating Support (the worksheet this
workflow binds against — no independent premium entry) and Endorsement Processing's
write-back/referral patterns, per the PRD. No auto-binding, no auto-resolved discrepancy
— human approves every bind order and resolves every flagged discrepancy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Ctx, Draft
from core.common.dtos import Decision as DecisionDTO
from core.common.dtos import OutputPackage as OutputPackageDTO
from core.common.enums import DecisionOutcome, ReviewAction
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.models import Submission
from core.review_queue import DefaultReviewQueueService
from verticals.mga.bind_issuance.engine import READY, BindDecision, BindIssuanceEngine
from verticals.mga.bind_issuance.fixtures import load_scenario
from verticals.mga.bind_issuance.schema import (
    ActivityEntry,
    AuthorityReconfirmationOut,
    BindDetail,
    BindRow,
    DiscrepancyOut,
    DownstreamTriggersOut,
    IssuanceReconciliationOut,
    PostBindObligationOut,
    StalenessCheckOut,
    SubjectivityOut,
    WorksheetReferenceOut,
    WriteBackOut,
)
from verticals.mga.models import MgaBindResult

WORKFLOW = "bind-issuance"

_ACTIONS = {
    "approve": ReviewAction.APPROVE,
    "send": ReviewAction.SEND,       # human-triggered carrier referral send
    "escalate": ReviewAction.ESCALATE,
}


class BindIssuanceService:
    def __init__(self) -> None:
        self.engine = BindIssuanceEngine()
        self.review_queue = DefaultReviewQueueService()
        self.audit = DefaultAuditService()

    async def process(
        self, session: AsyncSession, ctx: Ctx, scenario: str,
        request_override: dict[str, Any] | None = None,
    ) -> BindDetail:
        request = request_override or load_scenario(scenario)
        if request is None:
            raise KeyError(f"no bind/issuance fixture '{scenario}' for Workflow-06")

        sub = Submission(tenant_id=ctx.tenant_id, vertical=ctx.vertical,
                         external_ref=scenario, subject=request.get("submission_id", scenario),
                         status="binding")
        session.add(sub)
        await session.flush()

        decision = self.engine.decide(request)
        detail = self._build_detail(sub.id, scenario, request, decision)

        session.add(MgaBindResult(
            tenant_id=ctx.tenant_id, submission_id=sub.id, status=decision.status,
            authority_outcome=decision.authority_outcome,
            write_back_logged=decision.write_back_logged,
            issuance_status=decision.issuance_status,
            issuance_discrepancy_count=len(decision.issuance_discrepancies),
            post_bind_obligation_count=len(decision.post_bind_obligations)))

        out_dto = OutputPackageDTO(
            submission_id=sub.id,
            decision=DecisionDTO(
                outcome=(DecisionOutcome.PROCEED if decision.status == READY
                        else DecisionOutcome.DECLINE),
                score=None, rationale=decision.rationale),
            draft=Draft(text=decision.rationale, citations=[]),
            flags=[decision.status], missing_info=[],
            payload={"detail": detail.model_dump(by_alias=True)})
        await self.review_queue.enqueue(session, ctx, out_dto, WORKFLOW)

        await self.audit.record(session, ctx, AuditEntry(
            actor="ai", who="system", what=f"Bind order evaluated: {decision.status}",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"scenario": scenario, "status": decision.status}))
        await session.commit()
        return detail

    def _build_detail(
        self, sub_id: str, scenario: str, request: dict[str, Any], decision: BindDecision,
    ) -> BindDetail:
        now = datetime.now(UTC).isoformat()

        worksheet_ref = (
            WorksheetReferenceOut(worksheetId=decision.worksheet_id,
                                  worksheetDate=decision.worksheet_date,
                                  premium=decision.worksheet_premium)
            if decision.worksheet_id is not None else None)
        staleness = (
            StalenessCheckOut(daysSinceWorksheet=decision.days_since_worksheet,
                              exceedsThreshold=decision.exceeds_staleness_threshold,
                              materialUpdateLoggedSince=decision.material_update_logged_since)
            if decision.worksheet_id is not None else None)
        authority = (
            AuthorityReconfirmationOut(
                outcome=decision.authority_outcome, checkedPremium=decision.checked_premium,
                delegatedCeiling=decision.delegated_ceiling,
                referralDraftText=decision.referral_draft_text)
            if decision.authority_outcome is not None else None)

        return BindDetail(
            bindId=f"BND-{sub_id[-6:]}",
            submissionId=str(request.get("submission_id", sub_id)),
            namedInsured=str(request.get("named_insured", "")),
            worksheetReference=worksheet_ref,
            stalenessCheck=staleness,
            preBindSubjectivities=[
                SubjectivityOut(description=s.description, materiality=s.materiality,
                               status=s.status, lifecycleStage=s.lifecycle_stage)
                for s in decision.pre_bind_subjectivities
            ],
            authorityReconfirmation=authority,
            bindOrderStatus=decision.status,
            pasWriteBack=WriteBackOut(logged=decision.write_back_logged,
                                      bordereauSchemaValidated=decision.write_back_logged),
            issuanceReconciliation=IssuanceReconciliationOut(
                status=decision.issuance_status,
                discrepancyDetail=[
                    DiscrepancyOut(field=d.field, bound=d.bound, issued=d.issued)
                    for d in decision.issuance_discrepancies
                ]),
            postBindObligations=[
                PostBindObligationOut(
                    description=o.description, dueDate=o.due_date, status="open",
                    reminderDaysBefore=list(o.reminder_days_before))
                for o in decision.post_bind_obligations
            ],
            downstreamTriggersFired=DownstreamTriggersOut(
                bindConfirmation=decision.bind_confirmation_fired,
                policyDelivered=decision.policy_delivered_fired),
            rationale=decision.rationale,
            activity=[ActivityEntry(at=now, who="system (AI)",
                                    what=f"Bind order evaluated -> {decision.status}",
                                    ctx=scenario)],
        )

    # ── list / detail / act ──
    async def list_rows(self, session: AsyncSession, ctx: Ctx) -> list[BindRow]:
        stmt = (select(ReviewItemRow, OutputPackageRow)
                .join(OutputPackageRow,
                      col(ReviewItemRow.output_package_id) == col(OutputPackageRow.id))
                .where(col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                       col(ReviewItemRow.workflow) == WORKFLOW))
        rows: list[BindRow] = []
        for _item, pkg in (await session.execute(stmt)).all():
            payload = pkg.payload or {}
            if "detail" not in payload:
                continue
            d = BindDetail.model_validate(payload["detail"])
            premium = (f"${d.worksheetReference.premium:,.0f}"
                      if d.worksheetReference is not None else "—")
            rows.append(BindRow(id=pkg.submission_id, namedInsured=d.namedInsured,
                                premium=premium, status=d.bindOrderStatus))
        return rows

    async def get_detail(
        self, session: AsyncSession, ctx: Ctx, submission_id: str
    ) -> BindDetail | None:
        pkg = (await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.submission_id) == submission_id,
                col(OutputPackageRow.workflow) == WORKFLOW))
        ).scalars().first()
        if pkg is None or not pkg.payload:
            return None
        return BindDetail.model_validate(pkg.payload["detail"])

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
            raise KeyError(f"no bind-issuance review item for submission '{submission_id}'")
        result = await self.review_queue.act(session, ctx, item.id, review_action)
        await self.audit.record(session, ctx, AuditEntry(
            actor="human", who=ctx.user_id, what=f"{action} (role={ctx.role.value})",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"submission": submission_id, "action": action}))
        await session.commit()
        return {"id": result.id, "status": result.status.value}
