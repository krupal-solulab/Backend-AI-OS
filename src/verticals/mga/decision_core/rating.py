"""Quoting & Rating engine — the real, reusable premium-impact calculator.

This is the ONE engine every rating call in the MGA vertical must go through — including
Endorsement Processing's MEP-03, per that workflow's PRD: "do not reimplement rating
logic... this should call the existing Quoting & Rating Support engine directly." A
future ``verticals/mga/quoting_rating/`` workflow package wraps this same module with its
own router/schema for new-business quoting; this module is where the actual rating +
compliance logic lives, exactly like ``AppetiteEngine`` is the one appetite engine both
Submission Triage and Renewal Management call into.

Two compliance checks travel with every calculation, not just the arithmetic (this is
the whole point of calling this engine rather than reimplementing a shortcut):
  - QR-08 staleness: refuse to calculate against a superseded rate plan.
  - QR-03 bounds: refuse a calculated premium impact outside sane bounds.
"""

from __future__ import annotations

from dataclasses import dataclass

from verticals.mga.decision_core.rating_config import RatingConfig


@dataclass(frozen=True)
class RatingInput:
    class_code: str
    new_location_tiv: float | None = None
    days_remaining: int | None = None
    days_total: int | None = None
    # explicit overrides let a caller (e.g. a scenario with a pre-quoted rate) bypass
    # the lookup table without bypassing the compliance checks below.
    rate_plan_version: str | None = None
    filed_status: str | None = None
    base_rate_per_1000_tiv: float | None = None


@dataclass(frozen=True)
class RatingResult:
    blocked: bool
    reason: str | None
    rate_plan_version: str | None
    rate_plan_currency_check: str  # "CURRENT" | "STALE_BLOCKED" | "NOT_APPLICABLE"
    full_term_premium: float | None
    proration_factor: float | None
    premium_impact: float | None


class RatingEngine:
    """The Quoting & Rating Support engine. Calculates TIV-based premium impact with
    QR-08 staleness and QR-03 bounds enforcement — the two protections that must survive
    intact regardless of caller (new-business quoting or, here, mid-term endorsements)."""

    def __init__(self, config: RatingConfig | None = None) -> None:
        self.cfg = config or RatingConfig()

    def calculate(self, inp: RatingInput) -> RatingResult:
        plan = self._resolve_plan(inp)

        # ── QR-08: refuse to calculate against a superseded/stale rate plan ──
        if plan is None:
            return RatingResult(
                blocked=True, reason=f"No filed rate plan found for class {inp.class_code}",
                rate_plan_version=None, rate_plan_currency_check="STALE_BLOCKED",
                full_term_premium=None, proration_factor=None, premium_impact=None)
        version, filed_status, rate = plan
        if not self._is_current(filed_status):
            return RatingResult(
                blocked=True,
                reason=(f"Rate plan {version} is not current ({filed_status}) — "
                        "QR-08 blocks calculation pending rate plan refresh"),
                rate_plan_version=version, rate_plan_currency_check="STALE_BLOCKED",
                full_term_premium=None, proration_factor=None, premium_impact=None)

        # No TIV/exposure change → no premium impact to calculate (still QR-08 checked
        # above, since staleness must be surfaced even when the amount would be zero).
        if inp.new_location_tiv is None:
            return RatingResult(
                blocked=False, reason=None, rate_plan_version=version,
                rate_plan_currency_check="NOT_APPLICABLE", full_term_premium=None,
                proration_factor=None, premium_impact=None)

        full_term = (inp.new_location_tiv / 1000.0) * rate
        factor = self._proration_factor(inp.days_remaining, inp.days_total)
        impact = round(full_term * factor, 2)

        # ── QR-03: bounds enforcement — reject an out-of-range calculated impact ──
        if not (self.cfg.min_premium_impact <= impact <= self.cfg.max_premium_impact):
            return RatingResult(
                blocked=True,
                reason=(f"Calculated premium impact ${impact:,.2f} is outside the "
                        f"${self.cfg.min_premium_impact:,.0f}-"
                        f"${self.cfg.max_premium_impact:,.0f} bounds (QR-03)"),
                rate_plan_version=version, rate_plan_currency_check="CURRENT",
                full_term_premium=round(full_term, 2), proration_factor=factor,
                premium_impact=None)

        return RatingResult(
            blocked=False, reason=None, rate_plan_version=version,
            rate_plan_currency_check="CURRENT", full_term_premium=round(full_term, 2),
            proration_factor=factor, premium_impact=impact)

    def _resolve_plan(self, inp: RatingInput) -> tuple[str, str, float] | None:
        """Explicit inputs (a caller's own rate-plan lookup result) take precedence over
        the config table — the compliance checks below still run on whichever plan is
        resolved, so an explicit stale plan is blocked exactly like a looked-up one."""
        if inp.rate_plan_version and inp.filed_status and inp.base_rate_per_1000_tiv is not None:
            return inp.rate_plan_version, inp.filed_status, inp.base_rate_per_1000_tiv
        entry = self.cfg.rate_plans.get(inp.class_code)
        if entry is None:
            return None
        return entry.version, entry.filed_status, entry.base_rate_per_1000_tiv

    @staticmethod
    def _is_current(filed_status: str) -> bool:
        return "superseded" not in filed_status.lower()

    @staticmethod
    def _proration_factor(days_remaining: int | None, days_total: int | None) -> float:
        if not days_remaining or not days_total:
            return 1.0
        return round(days_remaining / days_total, 4)
