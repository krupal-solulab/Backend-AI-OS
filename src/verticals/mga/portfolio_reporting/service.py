"""Portfolio & Book Performance Reporting pipeline — ingest(dataset fixture, or eventually
a live pull from the result tables of Submission Triage, Quoting & Rating, Bind Order &
Issuance, Renewal Management, and Appetite Governance) -> analyze (PBR-01..07) -> review
-> audit. Like Appetite Governance & Audit Trail, this workflow makes no underwriting
decisions of its own — it aggregates and presents; every report routes to a
human-reviewed queue before being sent externally.
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
from verticals.mga.models import MgaPortfolioResult
from verticals.mga.portfolio_reporting.engine import PortfolioAnalysis, PortfolioEngine
from verticals.mga.portfolio_reporting.fixtures import load_scenario
from verticals.mga.portfolio_reporting.schema import (
    ActivityEntry,
    AppetiteExposureSectionOut,
    BrokerProductionOut,
    DataCompletenessOut,
    FunnelStageOut,
    GapOut,
    LossRatioOut,
    PortfolioReportDetail,
    PortfolioReportRow,
    RenewalRetentionOut,
)

WORKFLOW = "portfolio-reporting"

_ACTIONS = {
    "approve": ReviewAction.APPROVE,
    "send": ReviewAction.SEND,       # human-triggered external report send
    "escalate": ReviewAction.ESCALATE,
}


class PortfolioService:
    def __init__(self) -> None:
        self.engine = PortfolioEngine()
        self.review_queue = DefaultReviewQueueService()
        self.audit = DefaultAuditService()

    async def process(
        self, session: AsyncSession, ctx: Ctx, scenario: str,
        request_override: dict[str, Any] | None = None,
    ) -> PortfolioReportDetail:
        request = request_override or load_scenario(scenario)
        if request is None:
            raise KeyError(f"no portfolio-reporting fixture '{scenario}' for Workflow-08")

        sub = Submission(tenant_id=ctx.tenant_id, vertical=ctx.vertical,
                         external_ref=scenario, subject=request.get("period", scenario),
                         status="portfolio-reporting")
        session.add(sub)
        await session.flush()

        analysis = self.engine.analyze(request)
        detail = self._build_detail(sub.id, scenario, analysis)

        session.add(MgaPortfolioResult(
            tenant_id=ctx.tenant_id, submission_id=sub.id, status=analysis.status,
            completeness_status=analysis.completeness_status, gap_count=len(analysis.gaps),
            has_loss_ratio=analysis.loss_ratio is not None,
            has_renewal_retention=analysis.renewal_retention is not None,
            has_appetite_exposure=analysis.appetite_exposure is not None))

        out_dto = OutputPackageDTO(
            submission_id=sub.id,
            decision=DecisionDTO(outcome=DecisionOutcome.PROCEED, score=None,
                                 rationale=analysis.rationale),
            draft=Draft(text=analysis.rationale, citations=[]),
            flags=[analysis.status], missing_info=[],
            payload={"detail": detail.model_dump(by_alias=True)})
        await self.review_queue.enqueue(session, ctx, out_dto, WORKFLOW)

        await self.audit.record(session, ctx, AuditEntry(
            actor="ai", who="system", what=f"Portfolio report evaluated: {analysis.status}",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"scenario": scenario, "status": analysis.status}))
        await session.commit()
        return detail

    def _build_detail(
        self, sub_id: str, scenario: str, analysis: PortfolioAnalysis,
    ) -> PortfolioReportDetail:
        now = datetime.now(UTC).isoformat()

        return PortfolioReportDetail(
            reportId=f"PBR-{sub_id[-6:]}",
            period=analysis.period,
            dataCompleteness=DataCompletenessOut(
                status=analysis.completeness_status,
                gaps=[GapOut(sourceWorkflow=g.source_workflow, dateRange=g.date_range,
                             reason=g.reason,
                             crossReferencedFindingId=g.cross_referenced_finding_id)
                     for g in analysis.gaps]),
            funnel=[FunnelStageOut(stage=f.stage, count=f.count,
                                   pctOfPriorStage=f.pct_of_prior_stage)
                   for f in analysis.funnel],
            lossRatio=(
                LossRatioOut(
                    periodBasis=analysis.loss_ratio.period_basis,
                    earnedPremium=analysis.loss_ratio.earned_premium,
                    incurredLosses=analysis.loss_ratio.incurred_losses,
                    ratioPct=analysis.loss_ratio.ratio_pct,
                    lowVolumeFlag=analysis.loss_ratio.low_volume_flag,
                    singleEventDrivenFlag=analysis.loss_ratio.single_event_driven_flag,
                    detail=analysis.loss_ratio.detail)
                if analysis.loss_ratio is not None else None),
            renewalRetention=(
                RenewalRetentionOut(
                    eligible=analysis.renewal_retention.eligible,
                    retained=analysis.renewal_retention.retained,
                    nonRenewedUnderwritingDecision=(
                        analysis.renewal_retention.non_renewed_underwriting_decision),
                    lapsedNoDecision=analysis.renewal_retention.lapsed_no_decision,
                    retentionRatePct=analysis.renewal_retention.retention_rate_pct,
                    lineItems=analysis.renewal_retention.line_items)
                if analysis.renewal_retention is not None else None),
            brokerProduction=[
                BrokerProductionOut(
                    brokerAgency=b.broker_agency,
                    currentPeriodPremium=b.current_period_premium,
                    priorPeriodPremium=b.prior_period_premium, pctChange=b.pct_change,
                    significantDecline=b.significant_decline, detail=b.detail)
                for b in analysis.broker_production
            ],
            appetiteExposureSection=(
                AppetiteExposureSectionOut(
                    pulledFrom=analysis.appetite_exposure.pulled_from,
                    findingId=analysis.appetite_exposure.finding_id,
                    summary=analysis.appetite_exposure.summary,
                    lowVolumeFlag=analysis.appetite_exposure.low_volume_flag)
                if analysis.appetite_exposure is not None else None),
            status=analysis.status,
            rationale=analysis.rationale,
            activity=[ActivityEntry(at=now, who="system (AI)",
                                    what=f"Portfolio report evaluated -> {analysis.status}",
                                    ctx=scenario)],
        )

    # ── list / detail / act ──
    async def list_rows(self, session: AsyncSession, ctx: Ctx) -> list[PortfolioReportRow]:
        stmt = (select(ReviewItemRow, OutputPackageRow)
                .join(OutputPackageRow,
                      col(ReviewItemRow.output_package_id) == col(OutputPackageRow.id))
                .where(col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                       col(ReviewItemRow.workflow) == WORKFLOW))
        rows: list[PortfolioReportRow] = []
        for _item, pkg in (await session.execute(stmt)).all():
            payload = pkg.payload or {}
            if "detail" not in payload:
                continue
            d = PortfolioReportDetail.model_validate(payload["detail"])
            rows.append(PortfolioReportRow(id=pkg.submission_id, period=d.period,
                                           status=d.status))
        return rows

    async def get_detail(
        self, session: AsyncSession, ctx: Ctx, submission_id: str
    ) -> PortfolioReportDetail | None:
        pkg = (await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.submission_id) == submission_id,
                col(OutputPackageRow.workflow) == WORKFLOW))
        ).scalars().first()
        if pkg is None or not pkg.payload:
            return None
        return PortfolioReportDetail.model_validate(pkg.payload["detail"])

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
            raise KeyError(f"no portfolio-reporting review item for submission '{submission_id}'")
        result = await self.review_queue.act(session, ctx, item.id, review_action)
        await self.audit.record(session, ctx, AuditEntry(
            actor="human", who=ctx.user_id, what=f"{action} (role={ctx.role.value})",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"submission": submission_id, "action": action}))
        await session.commit()
        return {"id": result.id, "status": result.status.value}
