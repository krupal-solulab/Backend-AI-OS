"""Rating engine thresholds — DATA, not code (mirrors ``config.py``/``AppetiteConfig``).

Placeholder values sized from the Workflow_4 endorsement dataset's scenario_02 (Meridian
Self Storage: $2.15/$1,000 TIV, 61220) and scenario_06 (Apex Roofing: 96065, superseded
rate plan), plus the Workflow-05 quoting/rating dataset's class+state filed plans. Real
filed rate tables replace these once available — this module's shape (lookup-by-key,
bounds, staleness) is what callers (Quoting & Rating Support, Endorsement Processing)
depend on, not the specific numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RatePlanEntry:
    version: str
    filed_status: str  # e.g. "currently filed and approved" | "SUPERSEDED - ..."
    base_rate_per_1000_tiv: float


@dataclass(frozen=True)
class FiledRatePlan:
    """A QR-01 rate plan: filed per class-code-and-state combination (PRD Section 7.1)."""

    version: str
    filed_status: str  # "currently filed and approved" | "SUPERSEDED - ..."
    base_rate_per_1000_exposure: float
    allowed_credit_debit_range_pct: tuple[float, float]  # (min, max) e.g. (-15, 25)
    minimum_premium: float


@dataclass(frozen=True)
class RatingConfig:
    # class_code -> current filed rate plan (endorsement TIV-delta calc; state-agnostic
    # placeholder). Used only by calculate() / Endorsement Processing — unchanged.
    rate_plans: dict[str, RatePlanEntry] = field(default_factory=lambda: {
        "61220": RatePlanEntry(
            version="MS-61220-2027.1", filed_status="currently filed and approved",
            base_rate_per_1000_tiv=2.15),
        "96065": RatePlanEntry(
            version="SC-96065-2026.2",
            filed_status="SUPERSEDED - newer version SC-96065-2027.1 filed and approved 2027-03-01",
            base_rate_per_1000_tiv=1.85),
    })
    # QR-03 bounds enforcement: a calculated premium impact outside this range is
    # rejected rather than silently applied (guards against bad TIV/rate inputs).
    min_premium_impact: float = 0.0
    max_premium_impact: float = 5_000_000.0

    # (class_code, state) -> filed rate plan (QR-01). Used by calculate_worksheet() /
    # Quoting & Rating Support. Placeholder values from the Workflow-05 dataset.
    filed_rate_plans: dict[tuple[str, str], FiledRatePlan] = field(default_factory=lambda: {
        ("97047", "SC"): FiledRatePlan(
            version="SC-97047-2027.1", filed_status="currently filed and approved",
            base_rate_per_1000_exposure=8.10, allowed_credit_debit_range_pct=(-15, 25),
            minimum_premium=5000),
        ("60010", "GA"): FiledRatePlan(
            version="GA-60010-2027.1", filed_status="currently filed and approved",
            base_rate_per_1000_exposure=6.85, allowed_credit_debit_range_pct=(-15, 25),
            minimum_premium=8000),
        # TN-alone entry (scenario_03): minimum_premium=15000 is that scenario's own data.
        ("71535", "TN"): FiledRatePlan(
            version="TN-71535-2027.1", filed_status="currently filed and approved",
            base_rate_per_1000_exposure=9.20, allowed_credit_debit_range_pct=(-15, 25),
            minimum_premium=15000),
        # NC/GA/SC entries (scenario_05, multi-state): the dataset states no minimum
        # premium for any of the 4 states in this multi-state case — 0 (no floor) is
        # correct here, not a borrowed value from TN's unrelated single-state scenario.
        ("71535", "NC"): FiledRatePlan(
            version="NC-71535-2027.1", filed_status="currently filed and approved",
            base_rate_per_1000_exposure=8.65, allowed_credit_debit_range_pct=(-15, 25),
            minimum_premium=0),
        ("71535", "GA"): FiledRatePlan(
            version="GA-71535-2027.1", filed_status="currently filed and approved",
            base_rate_per_1000_exposure=9.55, allowed_credit_debit_range_pct=(-15, 25),
            minimum_premium=0),
        ("71535", "SC"): FiledRatePlan(
            version="SC-71535-2027.1", filed_status="currently filed and approved",
            base_rate_per_1000_exposure=8.90, allowed_credit_debit_range_pct=(-15, 25),
            minimum_premium=0),
        ("47380", "TN"): FiledRatePlan(
            version="TN-47380-2027.1", filed_status="currently filed and approved",
            base_rate_per_1000_exposure=3.10, allowed_credit_debit_range_pct=(-15, 25),
            minimum_premium=1750),
        ("96065", "SC"): FiledRatePlan(
            version="SC-96065-2027.1", filed_status="currently filed and approved",
            base_rate_per_1000_exposure=16.75, allowed_credit_debit_range_pct=(-15, 25),
            minimum_premium=10000),
    })
    # QR-07 sanity check: flag when indicated premium differs from the benchmark
    # (prior expiring premium) by more than this fraction.
    benchmark_variance_threshold_pct: float = 0.25
    # QR-04 default minimum premium when a (class_code, state) has no filed plan entry
    # and no explicit minimum was supplied on the request.
    default_minimum_premium: float = 0.0
