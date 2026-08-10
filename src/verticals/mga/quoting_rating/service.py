"""Quoting & Rating Support pipeline — ingest(dataset fixture) → build a schedule
adjustment suggestion with grounding (QR-03/FR-4) → call the real RatingEngine directly
for the actual calculation (QR-01/02/03/04/06/07/08, never reimplemented) → itemized
worksheet → review → audit. No auto-finalization — the underwriter reviews the worksheet
and finalizes manually, same permanent boundary as every workflow in this project.
"""

from __future__ import annotations

import re
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
from verticals.mga.decision_core.rating import (
    RatingEngine,
    StateExposure,
    WorksheetInput,
    WorksheetResult,
)
from verticals.mga.models import MgaQuotingResult
from verticals.mga.quoting_rating.config import QuotingConfig
from verticals.mga.quoting_rating.fixtures import load_scenario
from verticals.mga.quoting_rating.schema import (
    ActivityEntry,
    BenchmarkComparisonOut,
    StateCalculationOut,
    WorksheetDetail,
    WorksheetRow,
)

WORKFLOW = "quoting-rating"

_ACTIONS = {
    "approve": ReviewAction.APPROVE,
    "send": ReviewAction.SEND,
    "escalate": ReviewAction.ESCALATE,
}


class QuotingService:
    def __init__(self) -> None:
        self.engine = RatingEngine()
        self.cfg = QuotingConfig()
        self.review_queue = DefaultReviewQueueService()
        self.audit = DefaultAuditService()

    async def process(
        self, session: AsyncSession, ctx: Ctx, scenario: str,
        request_override: dict[str, Any] | None = None,
    ) -> WorksheetDetail:
        request = request_override or load_scenario(scenario)
        if request is None:
            raise KeyError(f"no quoting fixture '{scenario}' for Workflow-05")

        sub = Submission(tenant_id=ctx.tenant_id, vertical=ctx.vertical,
                         external_ref=scenario, subject=request.get("submission_id", scenario),
                         status="quoting")
        session.add(sub)
        await session.flush()

        worksheet_input = self._build_worksheet_input(request)
        result = self.engine.calculate_worksheet(worksheet_input)
        detail = self._build_detail(sub.id, scenario, request, result)

        session.add(MgaQuotingResult(
            tenant_id=ctx.tenant_id, submission_id=sub.id, class_code=request.get("class_code", ""),
            status=result.status, total_indicated_premium=result.total_indicated_premium,
            benchmark_flagged=result.benchmark_flagged_for_review,
            any_adjustment_capped=any(s.adjustment_capped for s in result.state_calculations),
            any_minimum_applied=any(s.minimum_premium_applied for s in result.state_calculations)))

        out_dto = OutputPackageDTO(
            submission_id=sub.id,
            decision=DecisionDTO(
                outcome=(DecisionOutcome.PROCEED if result.status == "READY_FOR_REVIEW"
                        else DecisionOutcome.REQUEST_INFO),
                score=None, rationale=self._rationale(result)),
            draft=Draft(text=self._rationale(result), citations=[]),
            flags=[result.status], missing_info=[],
            payload={"detail": detail.model_dump(by_alias=True)})
        await self.review_queue.enqueue(session, ctx, out_dto, WORKFLOW)

        await self.audit.record(session, ctx, AuditEntry(
            actor="ai", who="system", what=f"Rating worksheet calculated: {result.status}",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"scenario": scenario, "status": result.status}))
        await session.commit()
        return detail

    def _build_worksheet_input(self, request: dict[str, Any]) -> WorksheetInput:
        class_code = str(request.get("class_code", ""))
        prior_premium = request.get("prior_expiring_premium")

        multi_state = request.get("multi_state_exposure")
        if multi_state:
            states = [
                StateExposure(
                    state=s["state"], exposure_amount=s["allocated_revenue"],
                    rate_plan_version=s.get("rate_plan_version"),
                    filed_status="currently filed and approved",
                    base_rate_per_1000_exposure=s.get("base_rate_per_1000_revenue"))
                for s in multi_state
            ]
            return WorksheetInput(class_code=class_code, states=states,
                                  prior_expiring_premium=prior_premium)

        # stale rate-plan lookup result (scenario_06 shape) — an explicit per-state
        # override that carries its own (superseded) version/status/rate.
        lookup = request.get("rate_plan_lookup_result")
        exposure = request.get("exposure_basis", {})
        state = str(request.get("state", ""))
        if lookup is not None:
            states = [StateExposure(
                state=state, exposure_amount=exposure.get("amount", 0.0),
                rate_plan_version=lookup.get("version_found"),
                filed_status=lookup.get("filed_status", ""),
                base_rate_per_1000_exposure=lookup.get("base_rate_per_1000_revenue_old_version"))]
            return WorksheetInput(class_code=class_code, states=states,
                                  prior_expiring_premium=prior_premium)

        # single-state, resolve against the config table (no explicit rate_plan in the
        # request beyond what's already filed — matches scenario_01/02/03/04's shape).
        suggested_pct, grounding = self._suggest_adjustment(request)
        requested_pct = self._requested_adjustment(request)
        states = [StateExposure(state=state, exposure_amount=exposure.get("amount", 0.0))]
        return WorksheetInput(
            class_code=class_code, states=states, requested_adjustment_pct=requested_pct,
            suggested_adjustment_pct=suggested_pct, adjustment_grounding=grounding,
            prior_expiring_premium=prior_premium)

    def _suggest_adjustment(self, request: dict[str, Any]) -> tuple[float | None, str | None]:
        """QR-03/FR-4: suggest a specific adjustment only when a grounded basis exists in
        already-extracted data — never suggest one without a stated reason."""
        factors = request.get("extracted_factors", {})
        trend = str(factors.get("loss_history_trend", "")).lower()
        if any(k in trend for k in self.cfg.improving_trend_keywords):
            basis = [factors.get("loss_history_trend", "")]
            pct = self.cfg.improving_credit_pct
            if factors.get("safety_program_documented"):
                basis.append("documented safety program")
                pct += self.cfg.safety_program_extra_credit_pct
            return pct, " + ".join(str(b) for b in basis)
        if any(k in trend for k in self.cfg.worsening_trend_keywords):
            return self.cfg.worsening_debit_pct, str(factors.get("loss_history_trend", ""))
        return None, None

    @staticmethod
    def _requested_adjustment(request: dict[str, Any]) -> float | None:
        """An underwriter's explicit requested adjustment, per the dataset's
        ``underwriter_note`` free-text field (scenario_03's shape)."""
        note = str(request.get("extracted_factors", {}).get("underwriter_note", ""))
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", note)
        return float(m.group(1)) if m else None

    @staticmethod
    def _rationale(result: WorksheetResult) -> str:
        if result.status == "BLOCKED_STALE_RATE_PLAN":
            reasons = [s.blocked_reason for s in result.state_calculations if s.blocked_reason]
            return "; ".join(reasons) or "Blocked — stale rate plan."
        parts = []
        for s in result.state_calculations:
            if s.adjustment_capped:
                parts.append(
                    f"{s.state}: requested {s.requested_adjustment_pct:+.0f}% exceeds the "
                    f"filed range — capped at {s.applied_adjustment_pct:+.0f}%.")
            if s.minimum_premium_applied:
                parts.append(f"{s.state}: minimum premium floor applied.")
        if result.benchmark_flagged_for_review:
            parts.append(
                "Indicated premium differs materially from the benchmark — flagged for review.")
        return " ".join(parts) or (
            "Straightforward, transparent worksheet — no adjustments or flags.")

    def _build_detail(
        self, sub_id: str, scenario: str, request: dict[str, Any], result: WorksheetResult,
    ) -> WorksheetDetail:
        now = datetime.now(UTC).isoformat()
        return WorksheetDetail(
            worksheetId=f"QR-{sub_id[-6:]}",
            submissionId=str(request.get("submission_id", sub_id)),
            namedInsured=str(request.get("named_insured", "")),
            classCode=str(request.get("class_code", "")),
            stateCalculations=[
                StateCalculationOut(
                    state=s.state, ratePlanVersionUsed=s.rate_plan_version,
                    ratePlanCurrencyCheck=s.rate_plan_currency_check,
                    allocatedExposure=s.allocated_exposure, basePremium=s.base_premium,
                    suggestedAdjustmentPct=s.suggested_adjustment_pct,
                    adjustmentGrounding=s.adjustment_grounding,
                    requestedAdjustmentPct=s.requested_adjustment_pct,
                    appliedAdjustmentPct=s.applied_adjustment_pct,
                    adjustmentCapped=s.adjustment_capped,
                    premiumAfterAdjustment=s.premium_after_adjustment,
                    minimumPremiumApplied=s.minimum_premium_applied,
                    finalStatePremium=s.final_state_premium, blockedReason=s.blocked_reason)
                for s in result.state_calculations
            ],
            totalIndicatedPremium=result.total_indicated_premium,
            benchmarkComparison=BenchmarkComparisonOut(
                priorPremium=result.benchmark_prior_premium,
                pctVariance=result.benchmark_pct_variance,
                flaggedForReview=result.benchmark_flagged_for_review),
            status=result.status,
            activity=[ActivityEntry(at=now, who="system (AI)",
                                    what=f"Worksheet calculated -> {result.status}", ctx=scenario)],
        )

    # ── list / detail / act ──
    async def list_rows(self, session: AsyncSession, ctx: Ctx) -> list[WorksheetRow]:
        stmt = (select(ReviewItemRow, OutputPackageRow)
                .join(OutputPackageRow,
                      col(ReviewItemRow.output_package_id) == col(OutputPackageRow.id))
                .where(col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                       col(ReviewItemRow.workflow) == WORKFLOW))
        rows: list[WorksheetRow] = []
        for _item, pkg in (await session.execute(stmt)).all():
            payload = pkg.payload or {}
            if "detail" not in payload:
                continue
            d = WorksheetDetail.model_validate(payload["detail"])
            total = (f"${d.totalIndicatedPremium:,.0f}"
                    if d.totalIndicatedPremium is not None else "—")
            rows.append(WorksheetRow(
                id=pkg.submission_id, namedInsured=d.namedInsured, classCode=d.classCode,
                states=", ".join(s.state for s in d.stateCalculations),
                totalIndicatedPremium=total, status=d.status))
        return rows

    async def get_detail(
        self, session: AsyncSession, ctx: Ctx, submission_id: str
    ) -> WorksheetDetail | None:
        pkg = (await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.submission_id) == submission_id,
                col(OutputPackageRow.workflow) == WORKFLOW))
        ).scalars().first()
        if pkg is None or not pkg.payload:
            return None
        return WorksheetDetail.model_validate(pkg.payload["detail"])

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
            raise KeyError(f"no quoting-rating review item for submission '{submission_id}'")
        result = await self.review_queue.act(session, ctx, item.id, review_action)
        await self.audit.record(session, ctx, AuditEntry(
            actor="human", who=ctx.user_id, what=f"{action} (role={ctx.role.value})",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"submission": submission_id, "action": action}))
        await session.commit()
        return {"id": result.id, "status": result.status.value}
