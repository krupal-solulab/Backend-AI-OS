"""Native CI-01..CI-05 logic. None of this fits the generic 6-check rules
engine — compound signal-aggregation and pattern-scoring reasoning
(Option-A precedent, same as every prior E&S workflow's own engine
module).

This PRD's own Section 0 calls this workflow the highest scope-creep risk
in the vertical at every prior mention (Market Matching §2.2, Quote
Comparison QC-03, Renewal Remarketing §2.2) — read every function here
with that framing: **the default, correct behavior is to produce
nothing.** The rule most likely to be built carelessly is CI-02
(class-level vs. account-specific reason scoring) — see
``classify_reason_scope``'s docstring for the exact distinction this
guards against.

Thresholds below (``MIN_TOTAL_OUTCOMES``, ``MIN_CLASS_LEVEL_INCONSISTENT``,
``RECENT_WINDOW``) are placeholders per this project's convention —
verified by hand against all 4 real dataset scenarios before being
treated as correct, not derived from real operational data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

MIN_TOTAL_OUTCOMES = 3
MIN_CLASS_LEVEL_INCONSISTENT = 2
RECENT_WINDOW = 3

ReasonScope = Literal["class_level", "account_specific", "unstated"]
PatternType = Literal["CONFIRMED_CONSISTENT", "GENUINE_INCONSISTENCY", "INSUFFICIENT_SIGNAL"]

_ACCOUNT_SPECIFIC_PATTERNS = [
    re.compile(r"this (specific )?account", re.IGNORECASE),
    re.compile(r"\bthis risk\b", re.IGNORECASE),
    re.compile(r"for this account", re.IGNORECASE),
]
_CLASS_LEVEL_PATTERNS = [
    re.compile(r"no longer (writ|accept)", re.IGNORECASE),
    re.compile(r"class capacity", re.IGNORECASE),
    re.compile(r"appetite (has )?chang", re.IGNORECASE),
    re.compile(r"no longer in appetite", re.IGNORECASE),
]


def classify_reason_scope(reason: str | None) -> ReasonScope:
    """CI-02's central distinction. A declined submission with an
    ACCOUNT-SPECIFIC stated reason (severity too high for *this* account)
    is fundamentally different evidence from one with a CLASS-LEVEL
    stated reason (carrier no longer writes this class at all) — only the
    latter counts toward a potential profile-change signal. An unstated/
    unrecognized reason counts toward NEITHER — it must not silently
    inflate class-level evidence, but also must not be treated as
    disqualifying on its own (Scenario 02's third decline has no reason
    captured, yet the other two still carry the pattern)."""
    if not reason or reason.strip().lower() == "no reason given":
        return "unstated"
    if any(p.search(reason) for p in _ACCOUNT_SPECIFIC_PATTERNS):
        return "account_specific"
    if any(p.search(reason) for p in _CLASS_LEVEL_PATTERNS):
        return "class_level"
    return "unstated"


@dataclass(frozen=True)
class EvidenceItem:
    submission_id: str
    outcome: str
    date: str
    stated_reason: str | None
    reason_scope: ReasonScope | None  # None when the outcome was consistent (no reason needed)


@dataclass(frozen=True)
class PatternResult:
    pattern_type: PatternType
    total_outcomes: int
    class_level_inconsistent_count: int
    evidence: list[EvidenceItem] = field(default_factory=list)


def score_pattern(
    outcomes: list[dict[str, Any]], *, min_total_outcomes: int = MIN_TOTAL_OUTCOMES
) -> PatternResult:
    """CI-02/CI-05: the central judgment call. See module docstring for
    the exact scenario-by-scenario trace this reproduces:
    - Scenario 01 (1 outcome): suppressed on volume alone, regardless of
      direction.
    - Scenario 02 (4 outcomes, 3 recent inconsistent, 2 class-level):
      GENUINE_INCONSISTENCY.
    - Scenario 03 (3 outcomes, 1 inconsistent but account-specific): that
      outcome contributes ZERO toward class-level evidence, regardless of
      ratio -> INSUFFICIENT_SIGNAL, never scored like Scenario 02.
    - Scenario 04 (4/4 consistent, sufficient volume): CONFIRMED_CONSISTENT.

    ``min_total_outcomes`` defaults to this module's own placeholder
    constant so every existing direct/test caller keeps byte-identical
    behavior; real callers (service.py) pass the real, configurable
    ``Settings.carrier_appetite_min_total_outcomes`` (FR-3) instead."""
    sorted_outcomes = sorted(outcomes, key=lambda o: o["date"])
    evidence = [
        EvidenceItem(
            submission_id=o["submission_id"],
            outcome=o["outcome"],
            date=o["date"],
            stated_reason=o.get("reason_given"),
            reason_scope=(
                None
                if o.get("consistent_with_profile")
                else classify_reason_scope(o.get("reason_given"))
            ),
        )
        for o in sorted_outcomes
    ]
    total = len(sorted_outcomes)

    if total < min_total_outcomes:
        return PatternResult("INSUFFICIENT_SIGNAL", total, 0, evidence)

    if all(o.get("consistent_with_profile") for o in sorted_outcomes):
        return PatternResult("CONFIRMED_CONSISTENT", total, 0, evidence)

    recent = sorted_outcomes[-RECENT_WINDOW:]
    class_level_inconsistent = [
        o
        for o in recent
        if not o.get("consistent_with_profile")
        and classify_reason_scope(o.get("reason_given")) == "class_level"
    ]
    count = len(class_level_inconsistent)
    if count >= MIN_CLASS_LEVEL_INCONSISTENT:
        return PatternResult("GENUINE_INCONSISTENCY", total, count, evidence)

    return PatternResult("INSUFFICIENT_SIGNAL", total, count, evidence)


_CONFIDENCE_TIERS = ["low", "medium", "high"]


def compute_metadata_refresh(current_confidence: str, as_of: date) -> dict[str, str]:
    """CI-03: the ONE piece of this workflow allowed to write back
    automatically, and the boundary must be exact — this function's
    return value contains ONLY these two keys, ever. Confidence is
    reaffirmed (or upgraded one tier, on genuinely consistent
    confirmation) — never downgraded here, since this path only runs on
    CONFIRMED_CONSISTENT evidence. `class_codes_accepted`/`excluded`/
    `premium_band`/`severity_ceiling` are never touched by this or any
    other function in this module — there is no code route here capable
    of writing them (FR-4)."""
    idx = (
        _CONFIDENCE_TIERS.index(current_confidence)
        if current_confidence in _CONFIDENCE_TIERS
        else 1
    )
    new_confidence = _CONFIDENCE_TIERS[min(idx + 1, len(_CONFIDENCE_TIERS) - 1)]
    return {"appetite_confidence": new_confidence, "appetite_last_updated": as_of.isoformat()}
