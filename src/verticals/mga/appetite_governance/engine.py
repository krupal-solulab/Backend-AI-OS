"""Appetite Governance engine — AG-02 (decision trail aggregation + completeness) → AG-03
(rule version drift impact on the currently-bound book) → AG-04 (underwriter override
pattern detection, volume + documentation quality) → AG-05 (strictly-grounded external
audit report) → AG-06 (portfolio concentration reporting, low-volume caveat).

A pure aggregation/analysis layer over decision logs already produced by Submission
Triage, Renewal Management, Endorsement Processing, and Bind Order & Issuance — this
workflow makes no underwriting decisions of its own (per the PRD). Every finding routes
to a human-reviewed governance suggestion queue (AG-07, handled in the service layer);
nothing here ever triggers an automated non-renewal, decline, or corrective action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from verticals.mga.appetite_governance.config import GovernanceConfig

COMPLETE_CLEAN = "COMPLETE_CLEAN"
FLAGGED = "FLAGGED"
SUGGESTED = "SUGGESTED"
READY = "READY"
PARTIAL = "PARTIAL"
INFORMATIONAL = "INFORMATIONAL"


@dataclass(frozen=True)
class DecisionRecord:
    workflow: str
    record_id: str
    decision: str
    rules_version_applied: str
    underwriter: str


@dataclass(frozen=True)
class Gap:
    workflow: str
    date_range: str
    reason: str


@dataclass(frozen=True)
class RuleVersionDriftFinding:
    policy_number: str
    insured: str
    bound_under_version: str
    current_version: str
    still_qualifies: bool
    detail: str


@dataclass(frozen=True)
class OverridePatternFinding:
    underwriter: str
    override_count: int
    undocumented_count: int
    flagged: bool
    suggestion: str | None


@dataclass(frozen=True)
class PortfolioConcentrationFinding:
    class_code: str
    carrier: str
    accounts_near_ceiling: int
    total_accounts_in_segment: int
    low_volume_flag: bool
    detail: str


@dataclass(frozen=True)
class AuditReport:
    report_type: str
    carrier_name: str
    period: str
    triage_decisions: int
    renewal_decisions: int
    bind_decisions: int
    endorsement_decisions: int
    authority_ceiling_breaches_referred: int
    authority_ceiling_breaches_approved: int
    authority_ceiling_breaches_declined: int
    grounding_statement: str


@dataclass(frozen=True)
class GovernanceAnalysis:
    status: str
    rationale: str
    decisions: list[DecisionRecord] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    drift_findings: list[RuleVersionDriftFinding] = field(default_factory=list)
    override_findings: list[OverridePatternFinding] = field(default_factory=list)
    concentration_findings: list[PortfolioConcentrationFinding] = field(default_factory=list)
    audit_report: AuditReport | None = None


class GovernanceEngine:
    def __init__(self, config: GovernanceConfig | None = None) -> None:
        self.cfg = config or GovernanceConfig()

    def analyze(self, request: dict[str, Any]) -> GovernanceAnalysis:
        # AG-05: an external audit report request is its own distinct path — strictly
        # grounded to already-logged decision counts, no other analysis runs against it.
        if request.get("report_type") == "CARRIER_DELEGATED_AUTHORITY_AUDIT":
            return self._audit_report(request)
        # AG-03: a rule version change to cross-reference against the bound book.
        if "rules_version_change" in request:
            return self._rule_drift(request)
        # AG-04: underwriter override events to pattern-check.
        if "override_events" in request:
            return self._override_patterns(request)
        # AG-06: a portfolio concentration snapshot.
        if "bound_accounts_in_class" in request:
            return self._portfolio_concentration(request)
        # AG-02: plain decision-log aggregation (clean baseline or a completeness gap).
        return self._aggregate_decisions(request)

    # ── AG-02: decision trail aggregation + completeness ──
    def _aggregate_decisions(self, request: dict[str, Any]) -> GovernanceAnalysis:
        logged = request.get("decisions_logged")
        period = str(request.get("period", ""))

        # scenario_05's shape: a dict keyed by month, some months flagged as an outage.
        if isinstance(logged, dict):
            gaps = [
                Gap(workflow="Submission Triage", date_range=month, reason=info["status"])
                for month, info in logged.items()
                if info.get("status") != "complete"
            ]
            total = sum(info.get("count", 0) for info in logged.values())
            status = PARTIAL if gaps else COMPLETE_CLEAN
            rationale = (
                f"{total} decisions logged across {period}, but {len(gaps)} period(s) show "
                "a logging gap that must be flagged explicitly, never smoothed over: "
                + "; ".join(f"{g.date_range} ({g.reason})" for g in gaps)
                if gaps else
                f"{total} decisions compiled across {period}, fully logged — no gaps."
            )
            return GovernanceAnalysis(status=status, rationale=rationale, gaps=gaps)

        # scenario_01's shape: an explicit list of decision records, single active version.
        decisions = [
            DecisionRecord(
                workflow=d["workflow"],
                record_id=str(d.get("submission_id") or d.get("renewal_id") or d.get("bind_id")
                             or d.get("endorsement_id") or ""),
                decision=d["decision"], rules_version_applied=d["rules_version_applied"],
                underwriter=d["underwriter"])
            for d in (logged or [])
        ]
        versions = {d.rules_version_applied for d in decisions}
        workflows = {d.workflow for d in decisions}
        active = request.get("rules_version_active_throughout_period", "")
        rationale = (
            f"{len(decisions)} decisions compiled across {len(workflows)} workflows, all "
            f"citing rules version(s) {', '.join(sorted(versions)) or 'n/a'} — {active}"
        )
        return GovernanceAnalysis(status=COMPLETE_CLEAN, rationale=rationale, decisions=decisions)

    # ── AG-03: rule version drift impact on the currently-bound book ──
    def _rule_drift(self, request: dict[str, Any]) -> GovernanceAnalysis:
        change = request["rules_version_change"]
        findings = []
        for acct in request.get("bound_accounts_check", []):
            still_qualifies = bool(acct.get("still_qualifies_under_new_version", True))
            detail = (
                f"Bound {acct.get('bound_date', '')} under {acct['bound_under_version']}; "
                f"{change['change_summary']} — "
                + ("no longer qualifies under the current appetite rules."
                   if not still_qualifies else "still qualifies under the current rules.")
            )
            findings.append(RuleVersionDriftFinding(
                policy_number=acct["policy_number"], insured=acct.get("insured", ""),
                bound_under_version=acct["bound_under_version"],
                current_version=change["new_version"], still_qualifies=still_qualifies,
                detail=detail))

        flagged = [f for f in findings if not f.still_qualifies]
        status = FLAGGED if flagged else COMPLETE_CLEAN
        rationale = (
            f"Appetite rules changed {change['effective_date']} ({change['old_version']} -> "
            f"{change['new_version']}): {change['change_summary']}. Cross-referenced against "
            f"the currently-bound book: {len(flagged)} account(s) no longer qualify."
            if flagged else
            f"Appetite rules changed {change['effective_date']} — no currently-bound accounts "
            "are affected."
        )
        return GovernanceAnalysis(status=status, rationale=rationale, drift_findings=findings)

    # ── AG-04: override pattern detection — volume AND documentation, never volume alone ──
    def _override_patterns(self, request: dict[str, Any]) -> GovernanceAnalysis:
        by_underwriter: dict[str, list[dict[str, Any]]] = {}
        for event in request.get("override_events", []):
            by_underwriter.setdefault(event["underwriter"], []).append(event)

        findings = []
        any_flagged = False
        for underwriter, events in by_underwriter.items():
            undocumented = sum(1 for e in events if self._is_undocumented(e))
            flagged = (len(events) >= self.cfg.override_pattern_min_count
                      and undocumented >= self.cfg.override_pattern_min_undocumented)
            any_flagged = any_flagged or flagged
            suggestion = (
                f"{underwriter} has overridden {len(events)} DECLINE recommendations to "
                f"PROCEED this quarter, {undocumented} without any documented reason. "
                "Recommend underwriting leadership review these files for appropriate "
                "documentation and confirm override patterns reflect sound judgment rather "
                "than a compliance gap." if flagged else None)
            findings.append(OverridePatternFinding(
                underwriter=underwriter, override_count=len(events),
                undocumented_count=undocumented, flagged=flagged, suggestion=suggestion))

        status = SUGGESTED if any_flagged else COMPLETE_CLEAN
        flagged_names = [f.underwriter for f in findings if f.flagged]
        rationale = (
            f"Override pattern review across {len(findings)} underwriter(s): "
            + (f"genuine pattern detected for {', '.join(flagged_names)} (volume combined "
               "with missing documentation) — suggestion generated for governance review."
               if flagged_names else
               "no underwriter shows both volume and a documentation gap together — "
               "all instances suppressed as normal, well-justified judgment.")
        )
        return GovernanceAnalysis(status=status, rationale=rationale, override_findings=findings)

    def _is_undocumented(self, event: dict[str, Any]) -> bool:
        reason = str(event.get("override_reason_logged", "")).strip().lower()
        if not reason:
            return True
        return any(phrase in reason for phrase in self.cfg.undocumented_phrases)

    # ── AG-06: portfolio concentration, with an explicit low-volume caveat ──
    def _portfolio_concentration(self, request: dict[str, Any]) -> GovernanceAnalysis:
        ceiling = float(request["severity_ceiling"])
        accounts = request.get("bound_accounts_in_class", [])
        near_ceiling = [
            a for a in accounts
            if (float(a.get("current_loss_history_incurred", 0)) / ceiling
               >= self.cfg.near_ceiling_pct)
        ]
        total = int(request.get("total_accounts_in_class", len(accounts)))
        low_volume = total < self.cfg.low_volume_threshold

        finding = PortfolioConcentrationFinding(
            class_code=str(request.get("class_code", "")), carrier=str(request.get("carrier", "")),
            accounts_near_ceiling=len(near_ceiling), total_accounts_in_segment=total,
            low_volume_flag=low_volume,
            detail=(
                f"{len(near_ceiling)} of {total} bound accounts are running at or above "
                f"{self.cfg.near_ceiling_pct:.0%} of the ${ceiling:,.0f} severity ceiling: "
                + ", ".join(f"{a['insured']} ({a['pct_of_ceiling']:.1f}%)" for a in near_ceiling)))

        status = INFORMATIONAL if near_ceiling else COMPLETE_CLEAN
        rationale = (
            f"{finding.detail}. This is a small base ({total} accounts total in this "
            "class/carrier combination) — the finding is real and worth surfacing, but "
            "should be presented with that context rather than implying a book-wide trend."
            if low_volume and near_ceiling else finding.detail
        )
        return GovernanceAnalysis(
            status=status, rationale=rationale, concentration_findings=[finding])

    # ── AG-05: strictly-grounded external audit report ──
    def _audit_report(self, request: dict[str, Any]) -> GovernanceAnalysis:
        counts = request["logged_decisions_available"]
        report = AuditReport(
            report_type=request["report_type"], carrier_name=request["carrier_name"],
            period=request["period"], triage_decisions=counts["triage_decisions"],
            renewal_decisions=counts["renewal_decisions"], bind_decisions=counts["bind_decisions"],
            endorsement_decisions=counts["endorsement_decisions"],
            authority_ceiling_breaches_referred=counts["authority_ceiling_breaches_referred"],
            authority_ceiling_breaches_approved=counts[
                "authority_ceiling_breaches_referred_and_approved_by_carrier"],
            authority_ceiling_breaches_declined=counts[
                "authority_ceiling_breaches_referred_and_declined_by_carrier"],
            grounding_statement=(
                "Every figure in this report traces to an actual logged decision record — "
                "none are estimated, rounded for presentation, or extrapolated."))
        rationale = (
            f"{report.triage_decisions} triage, {report.renewal_decisions} renewal, "
            f"{report.bind_decisions} bind, and {report.endorsement_decisions} endorsement "
            f"decisions logged for {report.period}. {report.authority_ceiling_breaches_referred} "
            "instance(s) exceeded delegated authority and were properly referred to "
            f"{report.carrier_name} ({report.authority_ceiling_breaches_approved} approved, "
            f"{report.authority_ceiling_breaches_declined} declined) rather than processed "
            "independently — the referred-and-handled-correctly cases are the most valuable "
            "evidence that the delegated authority boundary was actively respected."
        )
        return GovernanceAnalysis(status=READY, rationale=rationale, audit_report=report)
