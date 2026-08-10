"""Appetite Governance & Audit Trail pipeline — ingest(dataset fixture, or eventually a
live pull from the audit_entry/result tables of Triage, Renewal, Endorsement, and Bind) →
analyze (AG-02..06) → governance suggestion queue (AG-07) → review → audit. This
workflow makes no underwriting decisions of its own — every finding is a flag or report
for human governance review; nothing here is ever auto-actioned against an account or
underwriter.
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
from verticals.mga.appetite_governance.engine import GovernanceAnalysis, GovernanceEngine
from verticals.mga.appetite_governance.fixtures import load_scenario
from verticals.mga.appetite_governance.schema import (
    ActivityEntry,
    AuditReportOut,
    DecisionRecordOut,
    DecisionTrailOut,
    GapOut,
    GovernanceDetail,
    GovernanceRow,
    GovernanceSuggestionOut,
    OverridePatternFindingOut,
    PortfolioConcentrationFindingOut,
    RuleVersionDriftFindingOut,
)
from verticals.mga.models import MgaGovernanceResult

WORKFLOW = "appetite-governance"

_ACTIONS = {
    "approve": ReviewAction.APPROVE,
    "send": ReviewAction.SEND,       # human-triggered external audit report send
    "escalate": ReviewAction.ESCALATE,
}


class GovernanceService:
    def __init__(self) -> None:
        self.engine = GovernanceEngine()
        self.review_queue = DefaultReviewQueueService()
        self.audit = DefaultAuditService()

    async def process(
        self, session: AsyncSession, ctx: Ctx, scenario: str,
        request_override: dict[str, Any] | None = None,
    ) -> GovernanceDetail:
        request = request_override or load_scenario(scenario)
        if request is None:
            raise KeyError(f"no appetite-governance fixture '{scenario}' for Workflow-07")

        sub = Submission(tenant_id=ctx.tenant_id, vertical=ctx.vertical,
                         external_ref=scenario, subject=request.get("period", scenario),
                         status="governance")
        session.add(sub)
        await session.flush()

        analysis = self.engine.analyze(request)
        detail = self._build_detail(sub.id, scenario, request, analysis)

        flagged_findings = (len(analysis.drift_findings)
                            + sum(1 for f in analysis.override_findings if f.flagged)
                            + len(analysis.concentration_findings))
        session.add(MgaGovernanceResult(
            tenant_id=ctx.tenant_id, submission_id=sub.id, status=analysis.status,
            gap_count=len(analysis.gaps), flagged_finding_count=flagged_findings,
            has_audit_report=analysis.audit_report is not None))

        out_dto = OutputPackageDTO(
            submission_id=sub.id,
            decision=DecisionDTO(outcome=DecisionOutcome.PROCEED, score=None,
                                 rationale=analysis.rationale),
            draft=Draft(text=analysis.rationale, citations=[]),
            flags=[analysis.status], missing_info=[],
            payload={"detail": detail.model_dump(by_alias=True)})
        await self.review_queue.enqueue(session, ctx, out_dto, WORKFLOW)

        await self.audit.record(session, ctx, AuditEntry(
            actor="ai", who="system", what=f"Governance analysis evaluated: {analysis.status}",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"scenario": scenario, "status": analysis.status}))
        await session.commit()
        return detail

    def _build_detail(
        self, sub_id: str, scenario: str, request: dict[str, Any], analysis: GovernanceAnalysis,
    ) -> GovernanceDetail:
        now = datetime.now(UTC).isoformat()
        period = str(request.get("period", scenario))

        suggestions = self._build_suggestion_queue(analysis)

        return GovernanceDetail(
            auditPeriodId=f"AG-{sub_id[-6:]}",
            period=period,
            decisionTrail=DecisionTrailOut(
                status=("PARTIAL" if analysis.gaps else "COMPLETE"),
                gaps=[GapOut(workflow=g.workflow, dateRange=g.date_range, reason=g.reason)
                     for g in analysis.gaps],
                decisions=[DecisionRecordOut(
                    workflow=d.workflow, recordId=d.record_id, decision=d.decision,
                    rulesVersionApplied=d.rules_version_applied, underwriter=d.underwriter)
                    for d in analysis.decisions]),
            ruleVersionDriftFindings=[
                RuleVersionDriftFindingOut(
                    policyNumber=f.policy_number, insured=f.insured,
                    boundUnderVersion=f.bound_under_version, currentVersion=f.current_version,
                    stillQualifies=f.still_qualifies, detail=f.detail)
                for f in analysis.drift_findings
            ],
            overridePatternFindings=[
                OverridePatternFindingOut(
                    underwriter=f.underwriter, overrideCount=f.override_count,
                    undocumentedCount=f.undocumented_count, flagged=f.flagged,
                    suggestion=f.suggestion)
                for f in analysis.override_findings
            ],
            portfolioConcentrationFindings=[
                PortfolioConcentrationFindingOut(
                    classCode=f.class_code, carrier=f.carrier,
                    accountsNearCeiling=f.accounts_near_ceiling,
                    totalAccountsInSegment=f.total_accounts_in_segment,
                    lowVolumeFlag=f.low_volume_flag, detail=f.detail)
                for f in analysis.concentration_findings
            ],
            governanceSuggestionQueue=suggestions,
            auditReport=(
                AuditReportOut(
                    reportType=analysis.audit_report.report_type,
                    carrierName=analysis.audit_report.carrier_name,
                    period=analysis.audit_report.period,
                    triageDecisions=analysis.audit_report.triage_decisions,
                    renewalDecisions=analysis.audit_report.renewal_decisions,
                    bindDecisions=analysis.audit_report.bind_decisions,
                    endorsementDecisions=analysis.audit_report.endorsement_decisions,
                    authorityCeilingBreachesReferred=(
                        analysis.audit_report.authority_ceiling_breaches_referred),
                    authorityCeilingBreachesApproved=(
                        analysis.audit_report.authority_ceiling_breaches_approved),
                    authorityCeilingBreachesDeclined=(
                        analysis.audit_report.authority_ceiling_breaches_declined),
                    groundingStatement=analysis.audit_report.grounding_statement)
                if analysis.audit_report is not None else None),
            status=analysis.status,
            rationale=analysis.rationale,
            activity=[ActivityEntry(at=now, who="system (AI)",
                                    what=f"Governance analysis evaluated -> {analysis.status}",
                                    ctx=scenario)],
        )

    @staticmethod
    def _build_suggestion_queue(analysis: GovernanceAnalysis) -> list[GovernanceSuggestionOut]:
        # AG-07: every finding from AG-03, AG-04, AG-06 routes to a human-reviewed queue.
        queue: list[GovernanceSuggestionOut] = []
        for i, drift in enumerate(analysis.drift_findings):
            if not drift.still_qualifies:
                queue.append(GovernanceSuggestionOut(
                    findingId=f"DRIFT-{i}", findingType="RULE_DRIFT", status="PENDING_REVIEW",
                    summary=drift.detail))
        for i, override in enumerate(analysis.override_findings):
            if override.flagged and override.suggestion:
                queue.append(GovernanceSuggestionOut(
                    findingId=f"OVERRIDE-{i}", findingType="OVERRIDE_PATTERN",
                    status="PENDING_REVIEW", summary=override.suggestion))
        for i, concentration in enumerate(analysis.concentration_findings):
            if concentration.accounts_near_ceiling > 0:
                queue.append(GovernanceSuggestionOut(
                    findingId=f"CONCENTRATION-{i}", findingType="PORTFOLIO_CONCENTRATION",
                    status="PENDING_REVIEW", summary=concentration.detail))
        return queue

    # ── list / detail / act ──
    async def list_rows(self, session: AsyncSession, ctx: Ctx) -> list[GovernanceRow]:
        stmt = (select(ReviewItemRow, OutputPackageRow)
                .join(OutputPackageRow,
                      col(ReviewItemRow.output_package_id) == col(OutputPackageRow.id))
                .where(col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                       col(ReviewItemRow.workflow) == WORKFLOW))
        rows: list[GovernanceRow] = []
        for _item, pkg in (await session.execute(stmt)).all():
            payload = pkg.payload or {}
            if "detail" not in payload:
                continue
            d = GovernanceDetail.model_validate(payload["detail"])
            finding_count = (len(d.ruleVersionDriftFindings)
                             + sum(1 for f in d.overridePatternFindings if f.flagged)
                             + len(d.portfolioConcentrationFindings))
            rows.append(GovernanceRow(id=pkg.submission_id, period=d.period,
                                      status=d.status, findingCount=finding_count))
        return rows

    async def get_detail(
        self, session: AsyncSession, ctx: Ctx, submission_id: str
    ) -> GovernanceDetail | None:
        pkg = (await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.submission_id) == submission_id,
                col(OutputPackageRow.workflow) == WORKFLOW))
        ).scalars().first()
        if pkg is None or not pkg.payload:
            return None
        return GovernanceDetail.model_validate(pkg.payload["detail"])

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
            raise KeyError(f"no appetite-governance review item for submission '{submission_id}'")
        result = await self.review_queue.act(session, ctx, item.id, review_action)
        await self.audit.record(session, ctx, AuditEntry(
            actor="human", who=ctx.user_id, what=f"{action} (role={ctx.role.value})",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"submission": submission_id, "action": action}))
        await session.commit()
        return {"id": result.id, "status": result.status.value}
