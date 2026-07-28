"""Native PR-01..PR-06 logic. None of this fits the generic 6-check rules
engine — aggregation/formatting/annotation reasoning (Option-A precedent,
same as every prior E&S workflow's own engine module).

PR-06 is this PRD's equivalent of every other workflow's highest-stakes
gate: this PRD's own risk register calls a single smoothed-over data gap
"the direct throughline back to the very first critique in this entire
project" (the original landing page's fabricated dashboard stats). Every
function here is written so that a missing figure renders as an explicit
gap, never an interpolated or silently-omitted one.

PR-04 (revenue attribution) has no field anywhere in this module or in
schema.py — the PRD explicitly says "do not build this rule from
assumption," and no scenario exercises it. Fully out of scope, not a
stub.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MIN_RELIABLE_VOLUME = 10  # placeholder — validate with a real design partner (PR-02)

_FUNNEL_STAGES = [
    ("submissions_received", "Submissions Received"),
    ("matched_to_carrier", "Matched to Carrier"),
    ("packages_assembled", "Packages Assembled"),
    ("quotes_received", "Quotes Received"),
    ("compared_and_selected", "Compared & Selected"),
    ("bound", "Bound"),
]


@dataclass(frozen=True)
class FunnelStage:
    stage: str
    count: int | None
    pct_of_prior_stage: float | None


@dataclass(frozen=True)
class DataGap:
    stage: str
    reason: str


@dataclass(frozen=True)
class FunnelResult:
    stages: list[FunnelStage]
    gaps: list[DataGap]
    overall_conversion_pct: float | None  # None whenever ANY gap exists — see build_funnel


def build_funnel(data: dict[str, Any]) -> FunnelResult:
    """PR-01/PR-06. A funnel-stage value that isn't an int is a logging
    gap (Scenario 03's ``compared_and_selected`` is the string
    "UNKNOWN - Quote Comparison workflow logging gap identified for 2
    weeks in August (system migration)") — its text becomes the gap's
    reason, its own percentage is never computed, and the NEXT stage's
    percentage is also never computed (its denominator is unknown) even
    though that next stage's raw count may still be reliable on its own
    (Scenario 03's Bound: 47 — bind logging itself is unaffected).

    ``overall_conversion_pct`` (submissions-received -> bound) is
    deliberately withheld whenever ANY gap exists anywhere in the funnel,
    even though its own two endpoints might both be numeric — showing a
    clean top-line conversion figure right next to an explicitly-flagged
    gap would undercut the gap's prominence, which is exactly the
    smoothing-over this rule exists to prevent.
    """
    stages: list[FunnelStage] = []
    gaps: list[DataGap] = []
    prior_count: int | None = None

    for key, label in _FUNNEL_STAGES:
        raw = data.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int):
            gaps.append(DataGap(stage=label, reason=str(raw)))
            stages.append(FunnelStage(stage=label, count=None, pct_of_prior_stage=None))
            prior_count = None
            continue

        pct = (
            round(raw / prior_count * 100, 1)
            if prior_count is not None and prior_count > 0
            else None
        )
        stages.append(FunnelStage(stage=label, count=raw, pct_of_prior_stage=pct))
        prior_count = raw

    first = stages[0].count if stages else None
    last = stages[-1].count if stages else None
    overall = (
        round(last / first * 100, 1)
        if not gaps and first and last is not None and first > 0
        else None
    )
    return FunnelResult(stages=stages, gaps=gaps, overall_conversion_pct=overall)


@dataclass(frozen=True)
class CarrierPerformance:
    carrier_name: str
    submissions_approached: int
    quote_rate: float
    bind_rate: float
    overall_hit_rate: float
    low_volume_flag: bool


def build_carrier_performance(
    carrier_activity: list[dict[str, Any]], *, min_reliable_volume: int = MIN_RELIABLE_VOLUME
) -> list[CarrierPerformance]:
    """PR-02. Ordered by ``submissions_approached`` DESCENDING — never by
    hit-rate. Sorting by rate alone would visually rank a low-volume
    carrier's inflated percentage (Vantage's 100% of 4) above a
    higher-volume, more reliable one (Ironclad's 63.6% of 22), which is
    exactly the false-precision problem this rule exists to catch."""
    results = []
    for c in carrier_activity:
        approached = int(c["submissions_approached"])
        quoted = int(c["quotes_issued"])
        binds = int(c["binds"])
        quote_rate = round(quoted / approached * 100, 1) if approached > 0 else 0.0
        bind_rate = round(binds / quoted * 100, 1) if quoted > 0 else 0.0
        overall_hit_rate = round(binds / approached * 100, 1) if approached > 0 else 0.0
        results.append(
            CarrierPerformance(
                carrier_name=c["carrier_name"],
                submissions_approached=approached,
                quote_rate=quote_rate,
                bind_rate=bind_rate,
                overall_hit_rate=overall_hit_rate,
                low_volume_flag=approached < min_reliable_volume,
            )
        )
    return sorted(results, key=lambda r: r.submissions_approached, reverse=True)


@dataclass(frozen=True)
class RemarketOutcome:
    account: str
    trigger_level: str
    outcome_type: str  # savings_identified | confirmation_value | not_remarketed
    savings_amount: float | None
    note: str | None


def categorize_remarket_outcome(outcome: dict[str, Any]) -> RemarketOutcome:
    """PR-05. Three genuinely distinct outcomes, never collapsed to a
    single savings figure:
    - ``not_remarketed``: the account was never actually shopped (e.g.
      suppressed by its own remarketing history per RR-08) — excluded
      from savings math entirely, not a $0 result.
    - ``confirmation_value``: the account WAS remarketed and the
      incumbent was confirmed the best option — $0 in direct savings but
      a real, valuable outcome (per Renewal Remarketing's own RR-04
      interpretation guide), never reported as a failure.
    - ``savings_identified``: a genuine quantified savings figure.
    """
    trigger = str(outcome.get("trigger", ""))
    savings = outcome.get("savings_identified")
    trigger_level = trigger.split(" - ")[0].strip()

    savings_note_excludes = isinstance(savings, str) and "not remarketed" in savings.lower()
    if trigger_level == "no_remarket" or savings_note_excludes:
        return RemarketOutcome(
            account=outcome["account"], trigger_level=trigger_level,
            outcome_type="not_remarketed", savings_amount=None, note=outcome.get("note"),
        )

    if isinstance(savings, int | float) and savings > 0:
        return RemarketOutcome(
            account=outcome["account"], trigger_level=trigger_level,
            outcome_type="savings_identified", savings_amount=float(savings),
            note=outcome.get("note"),
        )

    return RemarketOutcome(
        account=outcome["account"], trigger_level=trigger_level,
        outcome_type="confirmation_value", savings_amount=None, note=outcome.get("note"),
    )
