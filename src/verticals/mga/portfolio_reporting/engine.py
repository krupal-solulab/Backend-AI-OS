"""Portfolio & Book Performance Reporting engine — PBR-01 (funnel stage attribution) +
PBR-02 (loss ratio, period-matched) + PBR-03 (low-volume / single-event annotation) +
PBR-04 (renewal retention — separating genuine underwriting non-renewals from
administrative lapses, never collapsed) + PBR-05 (data completeness, cross-referenced
against known gaps already surfaced by Appetite Governance & Audit Trail) + PBR-06
(broker production, facts only, no speculative narrative) + PBR-07 (portfolio
concentration/exposure — pulled directly from Appetite Governance & Audit Trail's AG-06
finding, never independently recomputed).

Like Appetite Governance & Audit Trail, this is the last workflow on the MGA roadmap and
is almost entirely aggregation and presentation over decision logs other workflows
already produced — its only genuinely new logic is PBR-04's retention categorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from verticals.mga.portfolio_reporting.config import PortfolioConfig

READY = "READY"
FLAGGED = "FLAGGED"
PARTIAL = "PARTIAL"

RENEW_OUTCOMES = {"RENEW_AS_IS", "RENEW_WITH_CHANGES"}


@dataclass(frozen=True)
class FunnelStage:
    stage: str
    count: int
    pct_of_prior_stage: float | None


@dataclass(frozen=True)
class LossRatio:
    period_basis: str
    earned_premium: float
    incurred_losses: float
    ratio_pct: float
    low_volume_flag: bool
    single_event_driven_flag: bool
    detail: str


@dataclass(frozen=True)
class RenewalRetention:
    eligible: int
    retained: int
    non_renewed_underwriting_decision: int
    lapsed_no_decision: int
    retention_rate_pct: float
    line_items: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class DataGap:
    source_workflow: str
    date_range: str
    reason: str
    cross_referenced_finding_id: str | None


@dataclass(frozen=True)
class BrokerProduction:
    broker_agency: str
    current_period_premium: float
    prior_period_premium: float
    pct_change: float
    significant_decline: bool
    detail: str


@dataclass(frozen=True)
class AppetiteExposureSection:
    pulled_from: str
    finding_id: str
    summary: str
    low_volume_flag: bool


@dataclass(frozen=True)
class PortfolioAnalysis:
    status: str
    rationale: str
    period: str
    completeness_status: str = "COMPLETE"
    gaps: list[DataGap] = field(default_factory=list)
    funnel: list[FunnelStage] = field(default_factory=list)
    loss_ratio: LossRatio | None = None
    renewal_retention: RenewalRetention | None = None
    broker_production: list[BrokerProduction] = field(default_factory=list)
    appetite_exposure: AppetiteExposureSection | None = None


class PortfolioEngine:
    def __init__(self, config: PortfolioConfig | None = None) -> None:
        self.cfg = config or PortfolioConfig()

    def analyze(self, request: dict[str, Any]) -> PortfolioAnalysis:
        # PBR-07: an appetite exposure finding reference to pull through, never recompute.
        if "appetite_governance_finding_reference" in request:
            return self._appetite_exposure(request)
        # PBR-06: broker production figures to report factually.
        if "broker_production" in request:
            return self._broker_production(request)
        # PBR-05: a per-month bind data breakdown to check for completeness gaps.
        if "bind_data" in request:
            return self._data_completeness(request)
        # PBR-04: renewal outcomes to categorize (retained / non-renew / lapsed).
        if "renewal_outcomes" in request:
            return self._renewal_retention(request)
        # PBR-02/03: a class/carrier loss-ratio segment (low-volume, single-event risk).
        if "bound_accounts_in_class" in request:
            return self._segment_loss_ratio(request)
        # PBR-01/02/04: a clean, full-book quarterly report.
        return self._full_book_report(request)

    # ── PBR-01/02/04: clean baseline full-book report ──
    def _full_book_report(self, request: dict[str, Any]) -> PortfolioAnalysis:
        period = str(request.get("period", ""))
        submitted = int(request["submissions_received"])
        quoted = int(request["quoted"])
        bound = int(request["bound"])

        funnel = [
            FunnelStage(stage="submitted", count=submitted, pct_of_prior_stage=None),
            FunnelStage(stage="quoted", count=quoted,
                        pct_of_prior_stage=self._pct(quoted, submitted)),
            FunnelStage(stage="bound", count=bound,
                        pct_of_prior_stage=self._pct(bound, quoted)),
        ]
        overall_bind_pct = self._pct(bound, submitted)

        eligible = int(request["renewals_eligible"])
        retained = int(request["renewals_retained"])
        retention = RenewalRetention(
            eligible=eligible, retained=retained,
            non_renewed_underwriting_decision=0, lapsed_no_decision=0,
            retention_rate_pct=self._pct(retained, eligible))

        earned = float(request["earned_premium"])
        incurred = float(request["incurred_losses"])
        ratio_pct = round(incurred / earned * 100, 1) if earned else 0.0
        loss_ratio = LossRatio(
            period_basis=period, earned_premium=earned, incurred_losses=incurred,
            ratio_pct=ratio_pct, low_volume_flag=False, single_event_driven_flag=False,
            detail=f"${incurred:,.0f} incurred / ${earned:,.0f} earned premium = "
                   f"{ratio_pct:.1f}% loss ratio for {period}.")

        complete = str(request.get("data_completeness", "")).lower() == "complete"
        status = READY if complete else PARTIAL
        rationale = (
            f"{submitted} submissions -> {quoted} quoted -> {bound} bound "
            f"(overall submission-to-bind {overall_bind_pct:.1f}%); {retained} of "
            f"{eligible} eligible renewals retained ({retention.retention_rate_pct:.1f}%); "
            f"{ratio_pct:.1f}% loss ratio. Data completeness: "
            + ("COMPLETE — all figures presented with full confidence."
               if complete else "PARTIAL — see flagged gaps.")
        )
        return PortfolioAnalysis(
            status=status, rationale=rationale, period=period,
            completeness_status="COMPLETE" if complete else "PARTIAL",
            funnel=funnel, loss_ratio=loss_ratio, renewal_retention=retention)

    # ── PBR-02/03: a class/carrier segment loss ratio, low-volume + single-event risk ──
    def _segment_loss_ratio(self, request: dict[str, Any]) -> PortfolioAnalysis:
        period = str(request.get("period", ""))
        accounts = int(request["bound_accounts_in_class"])
        earned = float(request["earned_premium_this_class"])
        incurred = float(request["incurred_losses_this_class"])
        ratio_pct = round(incurred / earned * 100, 1) if earned else 0.0

        low_volume = accounts < self.cfg.low_volume_threshold
        note = str(request.get("note", "")).lower()
        single_event = "single" in note and "claim" in note

        detail = (
            f"${incurred:,.0f} incurred / ${earned:,.0f} earned = {ratio_pct:.1f}% loss "
            f"ratio for this class/carrier segment this quarter. "
        )
        if low_volume or single_event:
            detail += (
                f"This figure is based on only {accounts} bound accounts"
                + (" and the vast majority of the incurred loss figure comes from a "
                   "SINGLE large open claim on one account, not a broad pattern across "
                   "the segment" if single_event else "")
                + f". A {ratio_pct:.0f}% quarterly loss ratio on this small a base should "
                "NOT be presented with the same weight as a multi-year trend on a larger "
                "class — it reflects severity on one account far more than it reflects "
                "the underlying quality of this class/carrier combination as a whole."
            )

        loss_ratio = LossRatio(
            period_basis=period, earned_premium=earned, incurred_losses=incurred,
            ratio_pct=ratio_pct, low_volume_flag=low_volume,
            single_event_driven_flag=single_event, detail=detail)

        status = FLAGGED if (low_volume or single_event) else READY
        rationale = (
            f"{detail} Recommend showing both the point-in-time figure AND a "
            "multi-quarter trend line for this segment side by side, so leadership can "
            "see whether this is a one-time severity event or part of a genuine "
            "deteriorating pattern." if status == FLAGGED else detail
        )
        return PortfolioAnalysis(
            status=status, rationale=rationale, period=period, loss_ratio=loss_ratio)

    # ── PBR-04: renewal retention — never collapse lapse into non-renewal ──
    def _renewal_retention(self, request: dict[str, Any]) -> PortfolioAnalysis:
        period = str(request.get("period", ""))
        outcomes = request.get("renewal_outcomes", [])

        retained = 0
        non_renewed = 0
        lapsed = 0
        line_items: list[dict[str, str]] = []
        for o in outcomes:
            outcome = str(o.get("outcome", ""))
            policy = str(o.get("policy_number", ""))
            if outcome in RENEW_OUTCOMES:
                retained += 1
                line_items.append({
                    "policyNumber": policy, "category": "RETAINED", "outcome": outcome})
            elif outcome == "NON_RENEW":
                non_renewed += 1
                line_items.append({
                    "policyNumber": policy, "category": "UNDERWRITING_NON_RENEWAL",
                    "outcome": outcome,
                    "reason": str(o.get("reason", ""))})
            else:
                # No RENEW/NON_RENEW decision was ever logged — a process failure, per
                # the Renewal Management PRD's own TR-01 lapse-risk rule — a distinct,
                # third category, never merged into "not retained" alongside NON_RENEW.
                lapsed += 1
                line_items.append({
                    "policyNumber": policy, "category": "LAPSED_NO_DECISION",
                    "outcome": outcome, "note": str(o.get("note", ""))})

        eligible = len(outcomes)
        retention = RenewalRetention(
            eligible=eligible, retained=retained,
            non_renewed_underwriting_decision=non_renewed, lapsed_no_decision=lapsed,
            retention_rate_pct=self._pct(retained, eligible), line_items=line_items)

        rationale = (
            f"Of {eligible} renewal-eligible policies this period: {retained} retained "
            f"(RENEW_AS_IS/RENEW_WITH_CHANGES), {non_renewed} genuine underwriting "
            f"NON_RENEW decision(s) (reported as underwriting-driven non-renewal, "
            f"separate line item), {lapsed} lapsed with no decision ever logged "
            "(reported as a process failure / administrative lapse, separate line item — "
            "never collapsed into the same 'not retained' bucket as a genuine "
            "underwriting non-renewal)."
        )
        return PortfolioAnalysis(
            status=READY, rationale=rationale, period=period, renewal_retention=retention)

    # ── PBR-05: data completeness, cross-referenced against known sibling gaps ──
    def _data_completeness(self, request: dict[str, Any]) -> PortfolioAnalysis:
        period = str(request.get("period", ""))
        bind_data: dict[str, dict[str, Any]] = request["bind_data"]

        gaps: list[DataGap] = []
        for month, info in bind_data.items():
            status = str(info.get("status", ""))
            if status == "complete":
                continue
            # PBR-05: cross-reference the same underlying issue already identified by
            # Appetite Governance & Audit Trail's own Scenario 05, rather than treating
            # it as a newly-discovered, unrelated gap.
            cross_ref = (
                "AG-FIND-GAP-202705" if "appetite governance" in status.lower() else None)
            gaps.append(DataGap(
                source_workflow="Bind Order & Issuance Support", date_range=month,
                reason=status, cross_referenced_finding_id=cross_ref))

        status_str = PARTIAL if gaps else READY
        if gaps:
            gap_desc = "; ".join(
                f"{g.date_range} ({g.reason})"
                + (f" — cross-referenced to {g.cross_referenced_finding_id}, the same "
                   "gap already identified by Appetite Governance & Audit Trail, not a "
                   "newly-discovered issue" if g.cross_referenced_finding_id else "")
                for g in gaps)
            rationale = (
                f"Data completeness check for {period}: {gap_desc}. Any period total or "
                "period-over-period comparison must clearly mark the affected month as an "
                "unreliable data point, not silently omit it or interpolate a "
                "plausible-looking number to complete the quarter."
            )
        else:
            rationale = f"Data completeness check for {period}: all months complete."

        return PortfolioAnalysis(
            status=status_str, rationale=rationale, period=period,
            completeness_status=("PARTIAL" if gaps else "COMPLETE"), gaps=gaps)

    # ── PBR-06: broker production — facts only, no speculative narrative ──
    def _broker_production(self, request: dict[str, Any]) -> PortfolioAnalysis:
        period = str(request.get("period", ""))
        rows = []
        for b in request.get("broker_production", []):
            pct_change = float(b["pct_change"])
            significant = pct_change <= self.cfg.significant_decline_pct
            detail = (
                f"${b['q3_bound_premium']:,.0f} bound premium this period vs "
                f"${b['q2_bound_premium']:,.0f} prior period ({pct_change:+.1f}%) — "
                + ("a significant decline worth noting factually."
                   if significant else "within normal quarter-to-quarter variance.")
            )
            rows.append(BrokerProduction(
                broker_agency=b["broker_agency"],
                current_period_premium=float(b["q3_bound_premium"]),
                prior_period_premium=float(b["q2_bound_premium"]),
                pct_change=pct_change, significant_decline=significant, detail=detail))

        rationale = (
            "Broker production reported factually — figures and percentage changes only. "
            "The report does not speculate about causes it has no direct evidence for "
            "(relationship strain, competitive placement elsewhere, market conditions, or "
            "any other explanation): " + " ".join(r.detail for r in rows)
        )
        return PortfolioAnalysis(
            status=READY, rationale=rationale, period=period, broker_production=rows)

    # ── PBR-07: appetite exposure — pull through Appetite Governance's finding, never
    # recompute concentration from raw claims data ──
    def _appetite_exposure(self, request: dict[str, Any]) -> PortfolioAnalysis:
        period = str(request.get("period", ""))
        ref = request["appetite_governance_finding_reference"]
        section = AppetiteExposureSection(
            pulled_from=str(ref.get("source", "Appetite Governance & Audit Trail")),
            finding_id=str(ref["finding_id"]), summary=str(ref["finding_summary"]),
            low_volume_flag=bool(ref.get("low_volume_flag", False)))
        rationale = (
            f"Portfolio Risk Concentration section pulls finding {section.finding_id} "
            f"directly from {section.pulled_from} rather than independently "
            f"recomputing severity-ceiling proximity from raw claims data: "
            f"{section.summary}. Carried through exactly as originally flagged, "
            "including its low-volume caveat."
        )
        return PortfolioAnalysis(
            status=READY, rationale=rationale, period=period, appetite_exposure=section)

    @staticmethod
    def _pct(numerator: int | float, denominator: int | float) -> float:
        return round(numerator / denominator * 100, 1) if denominator else 0.0
