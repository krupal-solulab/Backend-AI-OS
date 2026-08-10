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

from dataclasses import dataclass, field

from verticals.mga.decision_core.rating_config import FiledRatePlan, RatingConfig


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


@dataclass(frozen=True)
class StateExposure:
    """One state's allocated exposure for a (possibly multi-state) worksheet request —
    QR-06: each state rates independently against its own filed plan, never blended."""

    state: str
    exposure_amount: float
    # explicit rate-plan override (e.g. a caller's own lookup result for multi-state
    # inputs that already carry per-state version/rate) bypasses the config table
    # without bypassing the QR-08 staleness check below. When overriding, the bounds
    # and minimum premium below apply too — if omitted, they fall back to the config
    # table's entry for this (class_code, state) if one exists, else a sane default
    # (never a DIFFERENT scenario's entry for the same class code in another state).
    rate_plan_version: str | None = None
    filed_status: str | None = None
    base_rate_per_1000_exposure: float | None = None
    allowed_credit_debit_range_pct: tuple[float, float] | None = None
    minimum_premium: float | None = None


@dataclass(frozen=True)
class WorksheetInput:
    class_code: str
    states: list[StateExposure] = field(default_factory=list)
    requested_adjustment_pct: float | None = None  # underwriter-requested credit/debit
    suggested_adjustment_pct: float | None = None  # system-suggested credit/debit (QR-03)
    adjustment_grounding: str | None = None
    prior_expiring_premium: float | None = None  # QR-07 benchmark


@dataclass(frozen=True)
class StateRatingResult:
    state: str
    rate_plan_version: str | None
    rate_plan_currency_check: str  # "CURRENT" | "STALE_BLOCKED"
    allocated_exposure: float
    base_premium: float | None
    suggested_adjustment_pct: float | None
    adjustment_grounding: str | None
    requested_adjustment_pct: float | None
    applied_adjustment_pct: float
    adjustment_capped: bool
    premium_after_adjustment: float | None
    minimum_premium_applied: bool
    final_state_premium: float | None
    blocked_reason: str | None


@dataclass(frozen=True)
class WorksheetResult:
    state_calculations: list[StateRatingResult]
    total_indicated_premium: float | None
    benchmark_prior_premium: float | None
    benchmark_pct_variance: float | None
    benchmark_flagged_for_review: bool
    status: str  # "READY_FOR_REVIEW" | "BLOCKED_STALE_RATE_PLAN"


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

    # ── Quoting & Rating Support: the full worksheet calculation (QR-01..QR-08) ──
    def calculate_worksheet(self, inp: WorksheetInput) -> WorksheetResult:
        """QR-01/02: resolve + apply the filed rate per state. QR-03: cap any credit/debit
        at the filed range, transparently. QR-04: enforce the minimum premium floor.
        QR-06: never blend states — each is rated independently. QR-07: flag large swings
        against the benchmark, informationally, after calculation. QR-08: block entirely
        on a stale rate plan, before any premium math runs."""
        results = [self._rate_one_state(inp, s) for s in inp.states]

        if any(r.blocked_reason for r in results):
            return WorksheetResult(
                state_calculations=results, total_indicated_premium=None,
                benchmark_prior_premium=inp.prior_expiring_premium,
                benchmark_pct_variance=None, benchmark_flagged_for_review=False,
                status="BLOCKED_STALE_RATE_PLAN")

        total = round(sum(r.final_state_premium or 0.0 for r in results), 2)
        variance, flagged = self._benchmark_check(total, inp.prior_expiring_premium)
        return WorksheetResult(
            state_calculations=results, total_indicated_premium=total,
            benchmark_prior_premium=inp.prior_expiring_premium,
            benchmark_pct_variance=variance, benchmark_flagged_for_review=flagged,
            status="READY_FOR_REVIEW")

    def _rate_one_state(self, inp: WorksheetInput, s: StateExposure) -> StateRatingResult:
        plan = self._resolve_filed_plan(inp.class_code, s)

        # ── QR-08: gate before any premium math, not a parallel/incidental check ──
        if plan is None:
            return StateRatingResult(
                state=s.state, rate_plan_version=None, rate_plan_currency_check="STALE_BLOCKED",
                allocated_exposure=s.exposure_amount, base_premium=None,
                suggested_adjustment_pct=None, adjustment_grounding=None,
                requested_adjustment_pct=None, applied_adjustment_pct=0.0,
                adjustment_capped=False, premium_after_adjustment=None,
                minimum_premium_applied=False, final_state_premium=None,
                blocked_reason=f"No filed rate plan found for class {inp.class_code} in {s.state}")
        if not self._is_current(plan.filed_status):
            return StateRatingResult(
                state=s.state, rate_plan_version=plan.version,
                rate_plan_currency_check="STALE_BLOCKED", allocated_exposure=s.exposure_amount,
                base_premium=None, suggested_adjustment_pct=None, adjustment_grounding=None,
                requested_adjustment_pct=None, applied_adjustment_pct=0.0,
                adjustment_capped=False, premium_after_adjustment=None,
                minimum_premium_applied=False, final_state_premium=None,
                blocked_reason=(f"Rate plan {plan.version} is not current ({plan.filed_status}) "
                               "— QR-08 blocks calculation pending rate plan refresh"))

        # ── QR-02: base premium from the already-extracted exposure ──
        base_premium = round((s.exposure_amount / 1000.0) * plan.base_rate_per_1000_exposure, 2)

        # ── QR-03: cap any requested/suggested adjustment at the filed range ──
        requested = inp.requested_adjustment_pct
        suggested = inp.suggested_adjustment_pct
        raw_pct = requested if requested is not None else suggested
        applied_pct, capped = self._cap_adjustment(raw_pct, plan)
        premium_after_adjustment = round(base_premium * (1 + applied_pct / 100.0), 2)

        # ── QR-04: minimum premium floor ──
        min_applied = premium_after_adjustment < plan.minimum_premium
        final_premium = plan.minimum_premium if min_applied else premium_after_adjustment

        return StateRatingResult(
            state=s.state, rate_plan_version=plan.version, rate_plan_currency_check="CURRENT",
            allocated_exposure=s.exposure_amount, base_premium=base_premium,
            suggested_adjustment_pct=suggested, adjustment_grounding=inp.adjustment_grounding,
            requested_adjustment_pct=requested, applied_adjustment_pct=applied_pct,
            adjustment_capped=capped, premium_after_adjustment=premium_after_adjustment,
            minimum_premium_applied=min_applied, final_state_premium=final_premium,
            blocked_reason=None)

    @staticmethod
    def _cap_adjustment(pct: float | None, plan: FiledRatePlan) -> tuple[float, bool]:
        if pct is None:
            return 0.0, False
        lo, hi = plan.allowed_credit_debit_range_pct
        if pct > hi:
            return hi, True
        if pct < lo:
            return lo, True
        return pct, False

    def _benchmark_check(
        self, total: float, prior_premium: float | None
    ) -> tuple[float | None, bool]:
        if not prior_premium:
            return None, False
        variance = round((total - prior_premium) / prior_premium, 4)
        flagged = abs(variance) > self.cfg.benchmark_variance_threshold_pct
        return variance, flagged

    def _resolve_filed_plan(self, class_code: str, s: StateExposure) -> FiledRatePlan | None:
        """Explicit per-state version/status/rate (e.g. a caller's own stale-plan lookup
        result, per scenario_06's shape) override those three fields; the filed bounds and
        minimum premium always come from the config table when an entry exists there, since
        those are regulatory data the request itself doesn't carry. QR-08 still runs against
        whichever version/status is resolved either way."""
        on_file = self.cfg.filed_rate_plans.get((class_code, s.state))
        version, filed_status = s.rate_plan_version, s.filed_status
        rate = s.base_rate_per_1000_exposure
        if version is None or filed_status is None or rate is None:
            return on_file
        bounds = (s.allowed_credit_debit_range_pct
                  or (on_file.allowed_credit_debit_range_pct if on_file else (-15.0, 25.0)))
        minimum = (s.minimum_premium if s.minimum_premium is not None else
                  (on_file.minimum_premium if on_file else self.cfg.default_minimum_premium))
        return FiledRatePlan(
            version=version, filed_status=filed_status, base_rate_per_1000_exposure=rate,
            allowed_credit_debit_range_pct=bounds, minimum_premium=minimum)

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
