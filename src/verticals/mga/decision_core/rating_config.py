"""Rating engine thresholds — DATA, not code (mirrors ``config.py``/``AppetiteConfig``).

Placeholder values sized from the Workflow_4 endorsement dataset's scenario_02 (Meridian
Self Storage: $2.15/$1,000 TIV, 61220) and scenario_06 (Apex Roofing: 96065, superseded
rate plan). Real filed rate tables replace these once available — this module's shape
(lookup-by-key, bounds, staleness) is what future callers (Quoting & Rating Support,
Endorsement Processing) depend on, not the specific numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RatePlanEntry:
    version: str
    filed_status: str  # e.g. "currently filed and approved" | "SUPERSEDED - ..."
    base_rate_per_1000_tiv: float


@dataclass(frozen=True)
class RatingConfig:
    # class_code -> current filed rate plan. Keyed by class code alone (state-agnostic
    # placeholder); a real rate table would key by (class_code, state).
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
