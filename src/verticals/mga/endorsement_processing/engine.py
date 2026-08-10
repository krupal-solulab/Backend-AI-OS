"""Endorsement Processing engine — MEP-01 (classification) → MEP-03 (premium impact,
direct call to the real RatingEngine) → MEP-02 (delegated authority recheck) →
MEP-04/06/07 (record/referral composition). An orchestration layer over already-built
engines, per the PRD: MEP-02 is the one genuinely new rule (a direct port of Submission
Triage's HR-01/HR-04 pattern); MEP-01 and MEP-03 are reuses, not reimplementations.

Outcome — exactly one of three, never conflated (FR-4):
  WITHIN_AUTHORITY | EXCEEDS_CEILING_REFERRAL_REQUIRED | CONFIRMED_EXCLUSION_DECLINED
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from verticals.mga.decision_core.rating import RatingEngine, RatingInput, RatingResult
from verticals.mga.endorsement_processing.config import EndorsementConfig

WITHIN_AUTHORITY = "WITHIN_AUTHORITY"
EXCEEDS_CEILING_REFERRAL_REQUIRED = "EXCEEDS_CEILING_REFERRAL_REQUIRED"
CONFIRMED_EXCLUSION_DECLINED = "CONFIRMED_EXCLUSION_DECLINED"
BLOCKED_STALE_RATE = "BLOCKED_STALE_RATE"


@dataclass(frozen=True)
class EndorsementDecision:
    classification: str  # ROUTINE | MATERIAL
    outcome: str  # one of the 4 constants above (BLOCKED_STALE_RATE is a pending state)
    rationale: str
    rating: RatingResult | None
    resulting_total_premium: float | None
    excluded_class_matched: str | None
    delegated_ceiling: float
    diff: list[dict[str, Any]]
    appetite: list[dict[str, Any]]
    schedule: list[str]
    carrier_referral_text: str | None
    premium_impact: float | None = None


class EndorsementEngine:
    def __init__(
        self, config: EndorsementConfig | None = None, rating: RatingEngine | None = None
    ) -> None:
        self.cfg = config or EndorsementConfig()
        self.rating = rating or RatingEngine()

    # ── MEP-01: classification ──
    def classify(self, change_type: str) -> str:
        if change_type in self.cfg.material_change_types:
            return "MATERIAL"
        if change_type in self.cfg.routine_change_types:
            return "ROUTINE"
        # unknown types default to MATERIAL (never assume routine on an unrecognized type)
        return "MATERIAL"

    def decide(self, request: dict[str, Any]) -> EndorsementDecision:
        change = request.get("requested_change", {})
        change_type = str(change.get("type", ""))
        classification = self.classify(change_type)

        # MEP-01 routine fast-track: administrative, but may still carry a modest
        # pre-quoted premium impact (e.g. a headcount update) — never assume zero, a
        # routine classification means "no binding-authority review required," not
        # "no premium effect."
        if classification == "ROUTINE" and change_type in self.cfg.routine_change_types:
            return self._decide_routine(request, change_type)
        return self._decide_material(request, change_type, classification)

    def _decide_routine(self, request: dict[str, Any], change_type: str) -> EndorsementDecision:
        change = request.get("requested_change", {})
        detail = str(change.get("detail", ""))
        authority = request.get("delegated_authority", {})
        ceiling = float(authority.get("premium_ceiling", 0) or 0)
        current_premium = float(authority.get("current_policy_premium", 0) or 0)
        label = change_type.replace("_", " ").title()

        routine_impact = self._resolve_premium_impact(request, None)
        resulting_total = current_premium + (routine_impact or 0.0)
        appetite = [{"rule": "MEP-01 change touches class/state/severity", "pass": True,
                    "hard": False, "detail": "No — administrative change, no exposure impact"}]
        schedule = [f"{label}: {detail}"]
        if routine_impact:
            schedule.append(f"Premium impact: ${routine_impact:,.2f}")

        impact_clause = (f", modest premium impact (${routine_impact:,.0f})" if routine_impact
                         else ", no premium/exposure impact")
        return EndorsementDecision(
            classification="ROUTINE", outcome=WITHIN_AUTHORITY,
            rationale=(f"{label} — administrative{impact_clause}. Ready to process within "
                       "the MGA's own binding authority; no carrier referral needed."),
            rating=None, resulting_total_premium=resulting_total, excluded_class_matched=None,
            delegated_ceiling=ceiling, diff=[], appetite=appetite, schedule=schedule,
            carrier_referral_text=None, premium_impact=routine_impact)

    def _decide_material(
        self, request: dict[str, Any], change_type: str, classification: str
    ) -> EndorsementDecision:
        change = request.get("requested_change", {})
        detail = str(change.get("detail", ""))
        authority = request.get("delegated_authority", {})
        ceiling = float(authority.get("premium_ceiling", 0) or 0)
        current_premium = float(authority.get("current_policy_premium", 0) or 0)
        appetite: list[dict[str, Any]] = []

        # ── MEP-02 (part 1): confirmed exclusion check — checked before rating ──
        excluded_classes = [str(c).lower() for c in authority.get("excluded_classes", [])]
        excluded_match = self._match_excluded(change_type, detail, excluded_classes)
        appetite.append({
            "rule": "MEP-02/HR-01 confirmed excluded class", "pass": excluded_match is None,
            "hard": True,
            "detail": (f"Matches excluded class '{excluded_match}'" if excluded_match
                      else "No excluded-class match")})
        if excluded_match is not None:
            carrier = authority.get("carrier", "the carrier")
            return EndorsementDecision(
                classification=classification, outcome=CONFIRMED_EXCLUSION_DECLINED,
                rationale=(f"'{excluded_match}' is explicitly on {carrier}'s excluded class list "
                           "within this MGA's delegated authority agreement — a confirmed "
                           "exclusion, not a discretionary ceiling case. Declined outright; no "
                           "carrier referral drafted. A separate placement may be needed."),
                rating=None, resulting_total_premium=None,
                excluded_class_matched=excluded_match, delegated_ceiling=ceiling, diff=[],
                appetite=appetite, schedule=[], carrier_referral_text=None)

        # ── MEP-03: premium impact via the real RatingEngine (never reimplemented) ──
        rating_result = self._calculate_impact(request)
        if rating_result is not None and rating_result.blocked:
            appetite.append({"rule": "MEP-03/QR-08 rate plan currency", "pass": False,
                             "hard": True, "detail": rating_result.reason or "Blocked"})
            return EndorsementDecision(
                classification=classification, outcome=BLOCKED_STALE_RATE,
                rationale=(f"{rating_result.reason} This is the same QR-08 check reused "
                           "wholesale from the Quoting & Rating Support engine, triggered "
                           "mid-term rather than at new-business quoting — not a new rule."),
                rating=rating_result, resulting_total_premium=None,
                excluded_class_matched=None, delegated_ceiling=ceiling, diff=[],
                appetite=appetite, schedule=[], carrier_referral_text=None)
        if rating_result is not None:
            appetite.append({
                "rule": "MEP-03/QR-08 rate plan currency", "pass": True, "hard": True,
                "detail": f"Rate plan {rating_result.rate_plan_version} current"})

        premium_impact = self._resolve_premium_impact(request, rating_result)

        # ── MEP-02 (part 2): delegated authority ceiling recheck ──
        resulting_total = current_premium + (premium_impact or 0.0)
        exceeds_ceiling = (premium_impact is not None and premium_impact > 0
                          and resulting_total > ceiling)
        appetite.append({
            "rule": "MEP-02/HR-04 delegated authority ceiling", "pass": not exceeds_ceiling,
            "hard": True,
            "detail": (f"${resulting_total:,.0f} vs ${ceiling:,.0f} ceiling"
                      if premium_impact else "No premium impact — ceiling not applicable")})

        diff: list[dict[str, Any]] = []
        if premium_impact:
            diff.append({"label": "Premium", "before": f"${current_premium:,.0f}",
                        "after": f"${resulting_total:,.0f}", "direction": "up"})

        if exceeds_ceiling:
            referral = self._draft_referral(request, premium_impact, resulting_total, ceiling)
            carrier = authority.get("carrier", "the carrier")
            return EndorsementDecision(
                classification=classification, outcome=EXCEEDS_CEILING_REFERRAL_REQUIRED,
                rationale=(f"Current premium (${current_premium:,.0f}) + calculated impact "
                           f"(${premium_impact:,.0f}) = ${resulting_total:,.0f} total, which "
                           f"EXCEEDS the ${ceiling:,.0f} delegated binding authority ceiling "
                           f"with {carrier} for this class — a discretionary boundary, not a "
                           "confirmed exclusion. Blocked from routine processing; carrier "
                           "referral required."),
                rating=rating_result, resulting_total_premium=resulting_total,
                excluded_class_matched=None, delegated_ceiling=ceiling, diff=diff,
                appetite=appetite, schedule=[], carrier_referral_text=referral,
                premium_impact=premium_impact)

        # ── within authority: draft the endorsement record (MEP-04) ──
        label = change_type.replace("_", " ").title()
        schedule = [f"{label}: {detail}"]
        if premium_impact:
            schedule.append(f"Pro-rata additional premium: ${premium_impact:,.2f}")
        else:
            schedule.append("No premium change")
        rationale = (f"New total policy premium (${resulting_total:,.0f}) remains within the "
                    f"${ceiling:,.0f} delegated ceiling. Ready to process within authority; "
                    "no carrier referral needed." if premium_impact else
                    "Within authority; no premium impact to check against the ceiling.")
        return EndorsementDecision(
            classification=classification, outcome=WITHIN_AUTHORITY, rationale=rationale,
            rating=rating_result, resulting_total_premium=resulting_total,
            excluded_class_matched=None, delegated_ceiling=ceiling, diff=diff,
            appetite=appetite, schedule=schedule, carrier_referral_text=None,
            premium_impact=premium_impact)

    # ── MEP-03: call the real RatingEngine directly (never reimplemented) ──
    def _calculate_impact(self, request: dict[str, Any]) -> RatingResult | None:
        authority = request.get("delegated_authority", {})
        class_code = str(authority.get("class_code") or authority.get("class_code_current") or "")
        change = request.get("requested_change", {})
        tiv = change.get("new_location_tiv")

        # explicit stale-rate-plan lookup result (scenario_06 shape) takes precedence —
        # it IS a rating-engine call result, just already resolved by the caller/dataset.
        lookup = request.get("rate_plan_lookup_result")
        if lookup is not None:
            return self.rating.calculate(RatingInput(
                class_code=class_code, new_location_tiv=tiv or 1.0,
                rate_plan_version=lookup.get("version_found"),
                filed_status=lookup.get("filed_status", ""),
                base_rate_per_1000_tiv=1.0))

        rate_ref = request.get("rate_plan_reference")
        if rate_ref is None or tiv is None:
            # nothing to rate — e.g. limit_increase carries a pre-quoted estimate instead
            return None

        days_elapsed = request.get("policy_term_days_elapsed")
        days_total = request.get("policy_term_total_days")
        days_remaining = (days_total - days_elapsed) if (days_elapsed is not None
                                                          and days_total is not None) else None
        return self.rating.calculate(RatingInput(
            class_code=class_code, new_location_tiv=float(tiv),
            days_remaining=days_remaining, days_total=days_total,
            rate_plan_version=rate_ref.get("version"),
            filed_status=rate_ref.get("filed_status", ""),
            base_rate_per_1000_tiv=rate_ref.get("base_rate_per_1000_tiv")))

    @staticmethod
    def _resolve_premium_impact(
        request: dict[str, Any], rating_result: RatingResult | None
    ) -> float | None:
        if rating_result is not None and rating_result.premium_impact is not None:
            return rating_result.premium_impact
        # pre-quoted estimates the dataset supplies directly (limit increase / headcount
        # update scenarios don't carry a TIV-based rate plan to look up).
        for key in ("estimated_additional_premium_for_increase", "premium_impact_estimated"):
            val = request.get(key)
            if val is not None:
                return float(val)
        return None

    @staticmethod
    def _match_excluded(change_type: str, detail: str, excluded_classes: list[str]) -> str | None:
        if change_type != "add_operations_class":
            return None
        text = detail.lower()
        for excluded in excluded_classes:
            if excluded in text or any(tok in text for tok in excluded.split()):
                return excluded
        return None

    @staticmethod
    def _draft_referral(
        request: dict[str, Any], premium_impact: float | None, resulting_total: float,
        ceiling: float,
    ) -> str:
        authority = request.get("delegated_authority", {})
        change = request.get("requested_change", {})
        carrier = authority.get("carrier", "the carrier")
        return (
            f"Referral to {carrier} underwriting — {request.get('named_insured', '')} "
            f"(policy {request.get('policy_number', '')}).\n\n"
            f"Current terms: premium ${authority.get('current_policy_premium', 0):,.0f}.\n"
            f"Requested change: {change.get('type', '')} — {change.get('detail', '')}.\n"
            f"Calculated premium impact: ${premium_impact:,.0f} "
            f"(resulting total ${resulting_total:,.0f}).\n"
            f"This exceeds our ${ceiling:,.0f} delegated binding authority ceiling for this "
            f"class — requesting your independent approval to proceed. Rationale: change is "
            f"otherwise within appetite and supported by the account's standing."
        )
