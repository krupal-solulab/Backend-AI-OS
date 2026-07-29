"""Native EP-01..EP-06 classification/reconciliation logic. None of this
fits the generic 6-check rules engine — compound classification and
reconciliation reasoning, Option-A precedent applied a fifth time.

EP-02's appetite recheck reuses Market Matching's DATA layer directly
(``verticals.es.decision_core.carrier_profiles`` — E&S-vertical-shared
infrastructure, not a sibling workflow's private internals, a different
boundary than the no-cross-import rule between workflow folders) but is
NEW, small, native logic here — not a call into Market Matching's own
``matching.py``, which is ranking logic built for a full carrier panel, not
rechecking one specific in-force carrier against one specific change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from verticals.es.decision_core.carrier_profiles import CarrierProfile

# EP-01: type-based classification first, then materiality within type.
# These types are ALWAYS routine, regardless of magnitude.
ALWAYS_ROUTINE_TYPES = {"additional_insured_endorsement", "address_correction"}
# These types are NEVER purely routine, regardless of how small they look.
NEVER_ROUTINE_TYPES = {"limit_increase", "add_operations_class", "add_location"}
# Everything else (e.g. employee_count_update) gets a materiality-within-type check.

# EP-03: which types are typically premium-bearing.
PREMIUM_BEARING_TYPES = {"limit_increase", "add_location", "employee_count_update"}
# EP-02: which types touch class, state, or severity exposure at all.
APPETITE_TOUCHING_TYPES = {"limit_increase", "add_operations_class", "add_location"}

# Placeholders (per this project's convention — validate with real brokers).
HEADCOUNT_MATERIALITY_PCT_THRESHOLD = 50.0
HEADCOUNT_MATERIALITY_ABS_PREMIUM_THRESHOLD = 10_000.0
# EP-06/FR-10: "very close to expiration" placeholder threshold.
UNUSUAL_TIMING_NEAR_EXPIRATION_DAYS = 30

_HEADCOUNT_RE = re.compile(r"from\s+(\d+)\s+to\s+(\d+)", re.IGNORECASE)
_STATE_ABBR_RE = re.compile(r"\b([A-Z]{2})\b")
_KEYWORD_RE = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class ClassificationResult:
    classification: str  # ROUTINE | UNDERWRITING_REVIEW_REQUIRED
    reasoning: str
    percent_change: float | None = None
    absolute_change: float | None = None


@dataclass(frozen=True)
class AppetiteRecheckResult:
    applicable: bool
    outcome: str  # WITHIN_APPETITE | OUTSIDE_APPETITE | APPETITE_UNKNOWN | NOT_APPLICABLE
    detail: str
    state_licensing_clarification_needed: bool = False


@dataclass(frozen=True)
class ProrationInputs:
    days_elapsed: int
    days_remaining: int
    term_total_days: int
    unusual_timing_flag: str | None = None


@dataclass(frozen=True)
class Discrepancy:
    requested_item: str
    issued_item: str | None


def _parse_headcount_change(detail: str) -> tuple[float | None, float | None]:
    match = _HEADCOUNT_RE.search(detail)
    if not match:
        return None, None
    before, after = int(match.group(1)), int(match.group(2))
    if before == 0:
        return None, float(after - before)
    return (after - before) / before * 100, float(after - before)


def classify(change_type: str, detail: str, current_premium: float | None) -> ClassificationResult:
    """EP-01: type-based first, then materiality within type — never a
    single flat "how big is this" score across every type (per the
    interpretation guide's strongest warning)."""
    if change_type in NEVER_ROUTINE_TYPES:
        return ClassificationResult(
            "UNDERWRITING_REVIEW_REQUIRED",
            f"'{change_type}' is never purely routine, regardless of magnitude.",
        )
    if change_type in ALWAYS_ROUTINE_TYPES:
        return ClassificationResult(
            "ROUTINE", f"'{change_type}' is a standard administrative change."
        )
    if change_type == "employee_count_update":
        pct, abs_change = _parse_headcount_change(detail)
        material = (
            pct is not None
            and pct > HEADCOUNT_MATERIALITY_PCT_THRESHOLD
            and (current_premium or 0) > HEADCOUNT_MATERIALITY_ABS_PREMIUM_THRESHOLD
        )
        reasoning = (
            f"{pct:.0f}% headcount change ({abs_change:+.0f} employees) on a "
            f"${current_premium or 0:,.0f}-premium account — "
            + ("both the percentage AND absolute premium exceed the materiality "
               "threshold." if material else
               "percentage alone would look material, but the account's small "
               "absolute premium keeps this routine — percentage change is not "
               "assessed alone.")
        )
        return ClassificationResult(
            "UNDERWRITING_REVIEW_REQUIRED" if material else "ROUTINE",
            reasoning, percent_change=pct, absolute_change=abs_change,
        )
    return ClassificationResult(
        "UNDERWRITING_REVIEW_REQUIRED",
        f"unrecognized change type '{change_type}' — defaulting to review, never "
        "auto-routine for an unmodeled type.",
    )


def _keyword_overlap(text: str, class_code: str) -> bool:
    class_words = {w for w in _KEYWORD_RE.findall(class_code.lower()) if len(w) > 3}
    text_words = set(_KEYWORD_RE.findall(text.lower()))
    return bool(class_words & text_words)


def _class_appetite_status(
    detail: str, accepted: list[str], excluded: list[str]
) -> tuple[str, str]:
    for cls in excluded:
        if _keyword_overlap(detail, cls):
            return "OUTSIDE_APPETITE", f"'{cls}' is on the carrier's excluded-class list."
    for cls in accepted:
        if _keyword_overlap(detail, cls):
            return "WITHIN_APPETITE", f"matches the carrier's accepted class '{cls}'."
    return (
        "APPETITE_UNKNOWN",
        "this class/use appears on NEITHER the carrier's accepted nor excluded "
        "list — an absent entry is not a confirmed exclusion (same principle as "
        "Market Matching's MM-04) — must be confirmed directly with the carrier, "
        "never auto-processed or auto-rejected.",
    )


def appetite_recheck(
    change_type: str,
    detail: str,
    bound_context: dict[str, object],
    carrier_panel: list[CarrierProfile],
) -> AppetiteRecheckResult:
    """EP-02: three-outcome model. Prefers accepted/excluded class lists
    EMBEDDED directly in the bound_policy_context snapshot when present;
    falls back to the real Workflow_10 CarrierProfile panel otherwise (not
    every scenario embeds them — verified against the actual fixtures, not
    assumed)."""
    if change_type not in APPETITE_TOUCHING_TYPES:
        return AppetiteRecheckResult(
            applicable=False, outcome="NOT_APPLICABLE",
            detail="This change type does not touch class, state, or severity exposure.",
        )

    if change_type == "limit_increase":
        parts = ["Limit increase touches severity exposure on this account."]
        loss_history = bound_context.get("loss_history_context")
        if loss_history:
            parts.append(f"Existing loss history: {loss_history}.")
        severity_ceiling = bound_context.get("carrier_severity_ceiling")
        if isinstance(severity_ceiling, int | float):
            parts.append(
                f"Carrier's stated per-claim severity ceiling: ${float(severity_ceiling):,.0f} "
                "(does not itself block this change, but combined with the account's own "
                "history, warrants underwriter judgment rather than automated processing)."
            )
        return AppetiteRecheckResult(
            applicable=True, outcome="WITHIN_APPETITE", detail=" ".join(parts)
        )

    accepted = bound_context.get("carrier_accepted_classes")
    excluded = bound_context.get("carrier_excluded_classes")
    if accepted is None or excluded is None:
        carrier_id = bound_context.get("carrier_id")
        profile = next((c for c in carrier_panel if c.carrier_id == carrier_id), None)
        accepted = list(profile.class_codes_accepted) if profile else []
        excluded = list(profile.class_codes_excluded) if profile else []
    assert isinstance(accepted, list) and isinstance(excluded, list)

    outcome, class_detail = _class_appetite_status(detail, accepted, excluded)
    state_flag = False
    if change_type == "add_location":
        carrier_id = bound_context.get("carrier_id")
        profile = next((c for c in carrier_panel if c.carrier_id == carrier_id), None)
        states_licensed = list(profile.states_licensed) if profile else []
        match = _STATE_ABBR_RE.search(detail)
        state_flag = match is None or (
            bool(states_licensed) and match.group(1) not in states_licensed
        )
        if state_flag:
            class_detail += (
                " State licensing not confirmed for the new location — flag as a required "
                "clarification before proceeding; do not assume same-state."
            )
    return AppetiteRecheckResult(
        applicable=True, outcome=outcome, detail=class_detail,
        state_licensing_clarification_needed=state_flag,
    )


def _unusual_timing_flag(days_elapsed: int, days_remaining: int) -> str | None:
    """EP-06/FR-10: flag unusual timing on the requested effective date —
    never an independent premium calculation, just a surfaced warning."""
    if days_elapsed < 0:
        return (
            "Requested effective date falls before the policy's own effective date — "
            "out of term, confirm this is correct."
        )
    if days_remaining < 0:
        return (
            "Requested effective date falls after the policy's expiration — the policy "
            "would need to be renewed first, not endorsed."
        )
    if days_remaining < UNUSUAL_TIMING_NEAR_EXPIRATION_DAYS:
        return (
            f"Only {days_remaining} day(s) remain in the current term — an endorsement "
            "may not be the appropriate mechanism this close to expiration; consider "
            "handling at renewal instead."
        )
    return None


def proration_inputs(
    effective_date: date, expiration_date: date, reference_date: date
) -> ProrationInputs:
    """EP-06: surfaces correct TIMING INPUTS only — never an independently
    calculated premium figure (that stays the carrier's determination)."""
    term_total = (expiration_date - effective_date).days
    elapsed = (reference_date - effective_date).days
    remaining = term_total - elapsed
    return ProrationInputs(
        days_elapsed=elapsed,
        days_remaining=remaining,
        term_total_days=term_total,
        unusual_timing_flag=_unusual_timing_flag(elapsed, remaining),
    )


def reconcile_items(requested_items: list[str], issued_items: list[str]) -> list[Discrepancy]:
    """EP-05: ITEM-LEVEL reconciliation for multi-part requests — a
    genuinely different shape from Binder & Issuance's scalar-field
    equality check. Each requested item must appear (case-insensitive
    substring match) among the issued items; a partial fulfillment must
    never be treated as fully reconciled."""
    discrepancies = []
    issued_lower = [i.lower() for i in issued_items]
    for item in requested_items:
        if not any(item.lower() in issued or issued in item.lower() for issued in issued_lower):
            discrepancies.append(Discrepancy(requested_item=item, issued_item=None))
    return discrepancies
