"""Native comparison/recommendation engine: QC-01 (comparability), QC-03
(declination-consistency, lightweight per the approved plan), QC-06 (mode
selection), QC-07 (validity/urgency tracking). None of this fits the generic
6-check rules engine — it's genuinely new comparison reasoning (offers
against each other, not a submission against a fixed appetite standard, per
the PRD's own §8 architectural point) — entirely native, Option-A.

All thresholds (the 5-business-day validity default) are config, per this
project's convention that such numbers are placeholders pending real broker
validation, not hardcoded assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import uuid4

from verticals.es.decision_core.carrier_profiles import CarrierProfile
from verticals.es.workflows.quote_comparison.quote_parser import (
    ParsedResponse,
    parse_valid_through_date,
)

VALIDITY_URGENCY_THRESHOLD_DAYS = 5  # QC-07/FR-15's placeholder default


@dataclass(frozen=True)
class Quote:
    """One parsed response plus its assigned id/carrier_id — the unit
    ``comparison_engine`` reasons over."""

    quote_id: str
    parsed: ParsedResponse
    carrier_id: str | None = None


@dataclass(frozen=True)
class ComparabilityResult:
    directly_comparable: bool
    material_differences: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class UrgencyFlag:
    quote_id: str
    flag_type: str  # validity_window | material_subjectivity | dependency_unresolved
    detail: str


@dataclass(frozen=True)
class RecommendationResult:
    # SINGLE_RECOMMENDATION | MULTI_OPTION | SINGLE_QUOTE_URGENT | SINGLE_QUOTE_ROUTINE
    output_mode: str
    primary_quote_id: str | None
    reasoning_summary: str
    comparability: ComparabilityResult
    urgency_flags: list[UrgencyFlag]


def make_quotes(parsed_responses: list[ParsedResponse]) -> list[Quote]:
    return [Quote(quote_id=str(uuid4()), parsed=p) for p in parsed_responses]


def assess_comparability(quotes: list[Quote]) -> ComparabilityResult:
    """QC-01: quotes are directly comparable only if limits, both deductible
    types, and every endorsement's basis match exactly across all of them —
    never on premium alone."""
    if len(quotes) < 2:
        return ComparabilityResult(directly_comparable=True)

    diffs: list[str] = []
    first = quotes[0].parsed
    if any(q.parsed.limits != first.limits for q in quotes[1:]):
        diffs.append("limits")

    first_ded = first.deductibles
    if first_ded is not None:
        rest_ded = [q.parsed.deductibles for q in quotes[1:]]
        if any(d is None or d.all_perils != first_ded.all_perils for d in rest_ded):
            diffs.append("deductible (all perils)")
        if any(d is None or d.wind_hail != first_ded.wind_hail for d in rest_ded):
            diffs.append("deductible (wind/hail)")

    endorsement_types = {e.type for q in quotes for e in q.parsed.key_endorsements}
    for etype in endorsement_types:
        bases = {
            e.basis for q in quotes for e in q.parsed.key_endorsements if e.type == etype
        }
        if len(bases) > 1:
            diffs.append(f"endorsement structure ({etype})")

    return ComparabilityResult(directly_comparable=not diffs, material_differences=diffs)


def classify_declination_consistency(
    quote: Quote, carrier_panel: list[CarrierProfile]
) -> str:
    """QC-03, lightweight per the approved plan: log-only comparison of a
    declination's stated reason against the carrier's ALREADY-KNOWN appetite
    profile (severity ceiling / excluded classes) — no new data, no action
    taken beyond this field."""
    parsed = quote.parsed
    if parsed.response_type != "DECLINATION":
        return "unable_to_determine"
    profile = next(
        (c for c in carrier_panel if c.carrier_name == parsed.carrier_name), None
    )
    if profile is None or parsed.declination_reason_amount is None:
        return "unable_to_determine"
    if parsed.declination_reason_amount > profile.severity_ceiling.max_single_claim_incurred:
        return "consistent"
    return "inconsistent"


def _validity_days_remaining(quote_valid_through: str | None, as_of: date) -> int | None:
    valid = parse_valid_through_date(quote_valid_through)
    if valid is None:
        return None
    return (valid - as_of).days


def _urgency_flags_for(quote: Quote, as_of: date, threshold_days: int) -> list[UrgencyFlag]:
    flags: list[UrgencyFlag] = []
    remaining = _validity_days_remaining(quote.parsed.quote_valid_through, as_of)
    if remaining is not None and remaining <= threshold_days:
        flags.append(UrgencyFlag(
            quote_id=quote.quote_id, flag_type="validity_window",
            detail=f"{remaining} day(s) remaining until quote_valid_through "
            f"({quote.parsed.quote_valid_through})",
        ))
    for subj in quote.parsed.subjectivities:
        if subj.materiality != "material":
            continue
        if subj.is_dependency:
            flags.append(UrgencyFlag(
                quote_id=quote.quote_id, flag_type="dependency_unresolved",
                detail=f"{subj.description} — resolution status not trackable in v1 "
                "(no upstream bind-status signal exists yet)",
            ))
        else:
            flags.append(UrgencyFlag(
                quote_id=quote.quote_id, flag_type="material_subjectivity",
                detail=subj.description,
            ))
    return flags


def _is_urgent(quote: Quote, as_of: date, threshold_days: int) -> bool:
    remaining = _validity_days_remaining(quote.parsed.quote_valid_through, as_of)
    if remaining is not None and remaining <= threshold_days:
        return True
    return any(s.materiality == "material" for s in quote.parsed.subjectivities)


def _rank_score(quote: Quote, price_weight: float, subjectivity_penalty: float) -> float:
    """QC-04/FR-18: configurable ranking basis — price plus a $-equivalent
    penalty per material subjectivity, rather than a hardcoded single-factor
    (price-only) default. ``subjectivity_penalty=0.0`` (the default) makes
    this mathematically identical to a pure-premium sort — see
    ``core.config.Settings.quote_rank_subjectivity_penalty``."""
    premium = quote.parsed.premium if quote.parsed.premium is not None else float("inf")
    material_count = sum(1 for s in quote.parsed.subjectivities if s.materiality == "material")
    return price_weight * premium + subjectivity_penalty * material_count


def recommend(
    quotes: list[Quote],
    as_of: date,
    threshold_days: int = VALIDITY_URGENCY_THRESHOLD_DAYS,
    price_weight: float = 1.0,
    subjectivity_penalty: float = 0.0,
) -> RecommendationResult:
    """QC-06: mode selection. Never defaults to a false single winner out of
    a genuine trade-off (per the interpretation guide's own warning)."""
    viable = [q for q in quotes if q.parsed.response_type == "QUOTE"]
    urgency_flags = [f for q in viable for f in _urgency_flags_for(q, as_of, threshold_days)]

    if len(viable) >= 2:
        comparability = assess_comparability(viable)
        if not comparability.directly_comparable:
            return RecommendationResult(
                output_mode="MULTI_OPTION",
                primary_quote_id=None,
                reasoning_summary=(
                    "Quotes are not directly comparable on premium alone — material "
                    f"differences in {', '.join(comparability.material_differences)}. "
                    "Presenting as an explicit trade-off rather than a single winner."
                ),
                comparability=comparability,
                urgency_flags=urgency_flags,
            )
        ranked = sorted(
            viable, key=lambda q: _rank_score(q, price_weight, subjectivity_penalty)
        )
        primary, secondary = ranked[0], ranked[1]
        primary_material = [s for s in primary.parsed.subjectivities if s.materiality == "material"]
        if primary_material:
            summary = (
                f"{primary.parsed.carrier_name} is lower-premium but contingent on: "
                f"{'; '.join(s.description for s in primary_material)}. Present as primary "
                f"with {secondary.parsed.carrier_name} as the clean fallback if the "
                "contingency isn't cleared in time."
            )
        else:
            summary = (
                f"{primary.parsed.carrier_name} is the clear recommendation — directly "
                "comparable terms at the lower premium with no material subjectivity gating it."
            )
        return RecommendationResult(
            output_mode="SINGLE_RECOMMENDATION",
            primary_quote_id=primary.quote_id,
            reasoning_summary=summary,
            comparability=comparability,
            urgency_flags=urgency_flags,
        )

    if len(viable) == 1:
        quote = viable[0]
        urgent = _is_urgent(quote, as_of, threshold_days)
        return RecommendationResult(
            output_mode="SINGLE_QUOTE_URGENT" if urgent else "SINGLE_QUOTE_ROUTINE",
            primary_quote_id=quote.quote_id,
            reasoning_summary=(
                f"Only one bindable option ({quote.parsed.carrier_name})."
                + (" Flagged urgent — see urgency_flags." if urgent else " No urgency flagged.")
            ),
            comparability=ComparabilityResult(directly_comparable=True),
            urgency_flags=urgency_flags,
        )

    return RecommendationResult(
        output_mode="SINGLE_QUOTE_ROUTINE",
        primary_quote_id=None,
        reasoning_summary="No viable quotes — every carrier response received was a declination.",
        comparability=ComparabilityResult(directly_comparable=True),
        urgency_flags=[],
    )


def recompute_urgency_from_payload(
    payload: dict[str, Any], as_of: date, threshold_days: int = VALIDITY_URGENCY_THRESHOLD_DAYS,
) -> dict[str, Any]:
    """QC-07/FR-14: recomputes urgency AGAINST THE CURRENT DATE at read time
    (every GET, not just at ingestion) — a quote that looked fine on day one
    needs to re-check as its validity window approaches. Operates on the
    already-persisted payload dict directly (no re-parsing of raw email
    text) and returns an updated copy; never writes back to the DB — a pure
    read-time projection, per the approved plan's "no new scheduler" default.
    Only ``SINGLE_QUOTE_ROUTINE``/``SINGLE_QUOTE_URGENT`` are re-derived —
    comparability/multi-option logic doesn't change with elapsed time."""
    quotes = payload.get("quotes") or []
    urgency_flags: list[dict[str, Any]] = []
    single_quote_is_urgent = False

    for q in quotes:
        if q.get("response_type") != "QUOTE":
            continue
        remaining = _validity_days_remaining(q.get("quote_valid_through"), as_of)
        material_present = False
        if remaining is not None and remaining <= threshold_days:
            urgency_flags.append({
                "quote_id": q["quote_id"], "flag_type": "validity_window",
                "detail": f"{remaining} day(s) remaining until quote_valid_through "
                f"({q.get('quote_valid_through')})",
            })
            single_quote_is_urgent = True
        for subj in q.get("subjectivities") or []:
            if subj.get("materiality") != "material":
                continue
            material_present = True
            if subj.get("is_dependency"):
                urgency_flags.append({
                    "quote_id": q["quote_id"], "flag_type": "dependency_unresolved",
                    "detail": f"{subj['description']} — resolution status not trackable in "
                    "v1 (no upstream bind-status signal exists yet)",
                })
            else:
                urgency_flags.append({
                    "quote_id": q["quote_id"], "flag_type": "material_subjectivity",
                    "detail": subj["description"],
                })
        if material_present:
            single_quote_is_urgent = True

    updated = dict(payload)
    updated["urgency_flags"] = urgency_flags
    if payload.get("output_mode") in ("SINGLE_QUOTE_ROUTINE", "SINGLE_QUOTE_URGENT"):
        updated["output_mode"] = (
            "SINGLE_QUOTE_URGENT" if single_quote_is_urgent else "SINGLE_QUOTE_ROUTINE"
        )
    return updated
