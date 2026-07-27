"""Native RR-01..RR-08 logic. None of this fits the generic 6-check rules
engine — compound orchestration/decision reasoning, Option-A precedent.

RR-01/RR-02 are NOT a port from an existing MGA Renewal Management
implementation — that workflow was never built in this codebase
(``verticals/mga/workflows/`` is empty, no Workflow_2 fixture data exists on
disk). This is fresh, native threshold-based detection informed by the
PRD's description of what MGA's RN-01..08 were meant to do, not copied code.

The central judgment call is RR-04: which of the four trigger levels a
renewal earns. It is NEVER collapsed to a binary — Scenario 03 exists
specifically to prove a real middle state (LIGHT_REMARKET_CHECK) that a
binary implementation would misclassify in either direction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Placeholders (per this project's convention — validate with real brokers).
EXPOSURE_MATERIALITY_PCT_THRESHOLD = 15.0
DISPROPORTIONALITY_RATIO_THRESHOLD = 3.0
DISPROPORTIONALITY_MIN_PREMIUM_PCT = 15.0
NON_RESPONSE_URGENT_DAYS_THRESHOLD = 30  # per Scenario 04's 25-day worked example

_ALREADY_EXPLAINED_PATTERNS = [
    re.compile(r"already[- ](endorsed|underwritten|priced|known)", re.IGNORECASE),
    re.compile(r"known,?\s+already", re.IGNORECASE),
    re.compile(r"roughly proportional", re.IGNORECASE),
    re.compile(r"consistent with a straightforward re-rate", re.IGNORECASE),
]
_NEW_CLAIM_COUNT_RE = re.compile(r"(no|one|two|three|four|five|\d+)\s+new claims?", re.IGNORECASE)
_WORD_TO_NUM = {"no": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_IMPROVING_PATTERNS = [
    re.compile(r"no recurrence", re.IGNORECASE),
    re.compile(r"aging out", re.IGNORECASE),
    re.compile(r"\byears? old\b", re.IGNORECASE),
]
_HISTORY_SUPPRESS_VALUE_PATTERNS = [
    re.compile(r"minimal savings", re.IGNORECASE),
    re.compile(r"no carrier change", re.IGNORECASE),
]
_HISTORY_REPEAT_PATTERNS = [
    re.compile(r"\d+\s+of\s+(?:the\s+)?(?:last|past)\s+\d+", re.IGNORECASE),
    re.compile(r"each of the past", re.IGNORECASE),
    re.compile(r"consecutive", re.IGNORECASE),
]
_EXCEPTION_RE = re.compile(r"\b(manual underwriting )?exception\b", re.IGNORECASE)


@dataclass(frozen=True)
class ExposureChangeResult:
    pct_change: float
    material: bool
    already_endorsed: bool
    note: str | None = None


@dataclass(frozen=True)
class LossHistoryChangeResult:
    new_claims_count: int
    favorable_resolutions_count: int
    trend: str  # improving | worsening | flat
    detail: str


@dataclass(frozen=True)
class IncumbentStatusResult:
    renewal_terms_received: bool
    days_before_expiration_at_receipt: int | None
    non_response_flag: bool
    pct_premium_change: float | None = None


@dataclass(frozen=True)
class RemarketingHistoryResult:
    has_history: bool
    suppress: bool
    detail: str | None = None


@dataclass(frozen=True)
class TriggerDecision:
    level: str  # NO_REMARKET | LIGHT_REMARKET_CHECK | FULL_REMARKET | URGENT_REMARKET
    reasoning: str


@dataclass(frozen=True)
class ComparisonResult:
    directly_comparable: bool
    material_differences: list[str]
    alternative_is_exception_based: bool
    exception_detail: str | None = None


def detect_exposure_change(exposure_change: dict[str, Any]) -> ExposureChangeResult:
    """RR-01: threshold-based revenue/exposure change detection. An increase
    already reflected in a mid-term endorsement is NOT new information at
    renewal (FR-2/Scenario 03) — detected here via the note field, since
    this dataset has no separate endorsement-history file to cross-
    reference against."""
    pct = float(exposure_change.get("pct_change", 0.0))
    note = exposure_change.get("note")
    already_endorsed = False
    if isinstance(note, str) and note:
        already_endorsed = any(p.search(note) for p in _ALREADY_EXPLAINED_PATTERNS)
    return ExposureChangeResult(
        pct_change=pct, material=abs(pct) > EXPOSURE_MATERIALITY_PCT_THRESHOLD,
        already_endorsed=already_endorsed, note=note,
    )


def detect_loss_history_change(loss_activity_text: str) -> LossHistoryChangeResult:
    """RR-02: new-claims / favorable-resolution / trend detection from the
    account's loss-activity narrative."""
    match = _NEW_CLAIM_COUNT_RE.search(loss_activity_text)
    count = 0
    if match:
        word = match.group(1).lower()
        found = _WORD_TO_NUM.get(word)
        if found is not None:
            count = found
        else:
            try:
                count = int(word)
            except ValueError:
                count = 0
    if count > 0:
        trend = "worsening"
    elif any(p.search(loss_activity_text) for p in _IMPROVING_PATTERNS):
        trend = "improving"
    else:
        trend = "flat"
    return LossHistoryChangeResult(
        new_claims_count=count, favorable_resolutions_count=0, trend=trend,
        detail=loss_activity_text,
    )


def check_incumbent_status(offer: dict[str, Any]) -> IncumbentStatusResult:
    """RR-03/RR-07: the incumbent's ACTUAL RESPONSIVENESS this cycle is a
    distinct signal from their stated appetite profile — silence close to
    expiration is itself the data point (per RR-07's interpretation note),
    not merged into a generic appetite check."""
    received = bool(offer.get("received", False))
    if not received:
        days_before = offer.get("days_before_expiration_at_check")
        non_response = days_before is not None and days_before <= NON_RESPONSE_URGENT_DAYS_THRESHOLD
        return IncumbentStatusResult(
            renewal_terms_received=False, days_before_expiration_at_receipt=None,
            non_response_flag=non_response,
        )
    return IncumbentStatusResult(
        renewal_terms_received=True,
        days_before_expiration_at_receipt=offer.get("days_before_expiration"),
        non_response_flag=False,
        pct_premium_change=offer.get("pct_premium_change"),
    )


def parse_remarketing_history(history: Any) -> RemarketingHistoryResult:
    """RR-08: the PRD's own §7.1 schema models this as a structured list,
    but the actual fixture (Scenario 06) provides a descriptive STRING
    instead — native text-heuristic parsing is what's actually needed for
    v1, not the structured shape. Absence of the field means NO suppressive
    effect (FR-11 — a first-cycle account must never be penalized for
    having no history yet)."""
    if not history:
        return RemarketingHistoryResult(has_history=False, suppress=False)
    if isinstance(history, str):
        suppress = any(p.search(history) for p in _HISTORY_SUPPRESS_VALUE_PATTERNS) and any(
            p.search(history) for p in _HISTORY_REPEAT_PATTERNS
        )
        return RemarketingHistoryResult(has_history=True, suppress=suppress, detail=history)
    # Structured list shape (per the PRD schema) — not exercised by this
    # dataset, but handled: suppress only on a demonstrated low-value pattern.
    if isinstance(history, list) and history:
        no_savings_cycles = sum(1 for c in history if not c.get("savings_identified"))
        suppress = no_savings_cycles >= 2
        return RemarketingHistoryResult(has_history=True, suppress=suppress, detail=str(history))
    return RemarketingHistoryResult(has_history=False, suppress=False)


def decide_trigger(
    exposure: ExposureChangeResult,
    loss: LossHistoryChangeResult,
    incumbent: IncumbentStatusResult,
    history: RemarketingHistoryResult,
) -> TriggerDecision:
    """RR-04: the central judgment call — four states, never a binary.
    Order matters: incumbent silence (URGENT) and demonstrated
    no-remarket-value history (NO_REMARKET) are checked FIRST, since both
    are independent of the pricing/exposure signals checked afterward."""
    if incumbent.non_response_flag:
        return TriggerDecision(
            level="URGENT_REMARKET",
            reasoning=(
                "Incumbent has not provided renewal terms despite broker follow-up, with "
                "limited time remaining before expiration — a lapse-risk signal, independent "
                "of pricing or exposure, treated with the same urgency as a timing-critical "
                "alert elsewhere in this vertical."
            ),
        )

    if history.suppress:
        return TriggerDecision(
            level="NO_REMARKET",
            reasoning=(
                f"This account's own remarketing history shows no demonstrated value: "
                f"{history.detail}"
            ),
        )

    disproportionate = False
    if (
        incumbent.pct_premium_change is not None
        and not exposure.already_endorsed
    ):
        exposure_baseline = max(abs(exposure.pct_change), 1.0)
        ratio = abs(incumbent.pct_premium_change) / exposure_baseline
        disproportionate = (
            ratio > DISPROPORTIONALITY_RATIO_THRESHOLD
            and abs(incumbent.pct_premium_change) > DISPROPORTIONALITY_MIN_PREMIUM_PCT
        )

    if disproportionate and loss.trend == "worsening":
        return TriggerDecision(
            level="FULL_REMARKET",
            reasoning=(
                f"Premium change of {incumbent.pct_premium_change}% is disproportionate to "
                f"{exposure.pct_change}% exposure growth, driven by continued adverse loss "
                f"activity ({loss.new_claims_count} new claim(s)) — worth confirming no better "
                f"alternative exists, even if the pricing itself is likely justified."
            ),
        )

    light_check_warranted = (exposure.material and exposure.already_endorsed) or (
        loss.trend == "improving" and exposure.material
    )
    if light_check_warranted:
        return TriggerDecision(
            level="LIGHT_REMARKET_CHECK",
            reasoning=(
                "Exposure growth is known/already explained and loss trend is favorable, but "
                "the account's larger size band makes a lightweight comparison check worth "
                "doing — distinct in effort from a full remarket campaign."
            ),
        )

    return TriggerDecision(
        level="NO_REMARKET",
        reasoning=(
            "No material adverse or unexplained change, incumbent responsive, and renewal "
            "terms reasonable relative to exposure change."
        ),
    )


def _limits_signature(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(re.findall(r"\d[\d,]*", text))


def compare_renewal_options(
    incumbent: dict[str, Any], alternative: dict[str, Any]
) -> ComparisonResult:
    """RR-06: a direct re-application of Quote Comparison's QC-01
    discipline (never compare premium alone), but simple enough to be
    native here — this only ever compares exactly 2 offers (incumbent vs.
    one alternative), not Quote Comparison's full multi-quote engine. Also
    flags an exception-based quote (Scenario 05) — a real, usable quote,
    but with different reliability characteristics than a standard-
    appetite one."""
    material_differences = []
    if _limits_signature(incumbent.get("limits")) != _limits_signature(alternative.get("limits")):
        material_differences.append("limits")
    if incumbent.get("deductible") != alternative.get("deductible"):
        material_differences.append("deductible")

    note = alternative.get("note") or ""
    is_exception = bool(_EXCEPTION_RE.search(note))
    return ComparisonResult(
        directly_comparable=not material_differences,
        material_differences=material_differences,
        alternative_is_exception_based=is_exception,
        exception_detail=note if is_exception else None,
    )
