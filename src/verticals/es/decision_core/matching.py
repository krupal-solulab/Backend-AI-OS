"""E&S Matching/Ranking Engine (verticals/es/decision_core — NOT shared core).

Implements rules MM-01..MM-07 from the Workflow_10 dataset's
``RULE_ENGINE_INTERPRETATION_GUIDE.md`` as a three-tier structure:

1. Hard exclusion (MM-01 class code, MM-02 state licensing, MM-03 premium band,
   MM-04 explicit class exclusion) — the carrier does not appear in the ranked
   output at all.
2. Soft scoring (MM-05 severity ceiling, MM-06 completeness) — the carrier
   appears, ranked, with informational flags.
3. Independent (MM-07 diligent search) — computed once per submission,
   regardless of ranking outcome, including the zero-match case.

Design split (the agreed hybrid): premium-band (MM-03) and the loss-run-years +
required-document checks that feed MM-06 completeness run through the SHARED
``core.rules_engine`` — one ``RuleSet``/``RuleVersion`` PER CARRIER (see
``seed_rules.py``), evaluated via its real, current JSON shape
(``params.value`` for min/max, plain ``required`` for doc-presence flags).
That's genuine reuse of the generic evaluator, not a bypass.

Semantic class-code scope matching (MM-01/04), multi-state licensing (MM-02),
the per-class hard/soft severity distinction (MM-05), the weighted composite
score, and MM-07 do NOT reduce cleanly to the generic evaluator's field
checks (list/scope-aware logic + cross-claim aggregation) and are implemented
natively here — exactly the "thin per-vertical decision layer" CORE_MODULES.md
reserves for this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import Ctx, Decision, ExtractedModel, ExtractedValue
from core.common.enums import DecisionOutcome
from core.rules_engine import DefaultRulesEngine
from verticals.es.decision_core.carrier_profiles import CarrierProfile

# ── US state name -> abbreviation (only what this dataset's submissions use;
# extend if later datasets introduce more states). ──────────────────────────
_STATE_ABBR: dict[str, str] = {
    "south carolina": "SC", "north carolina": "NC", "georgia": "GA",
    "tennessee": "TN", "virginia": "VA", "alabama": "AL", "mississippi": "MS",
    "florida": "FL", "oregon": "OR",
}

_CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3}
_LOSS_RUN_YEARS_RE = re.compile(r"(\d+)[\s-]*year", re.IGNORECASE)
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def ruleset_key(carrier_id: str) -> str:
    return f"es_carrier_{carrier_id.lower()}"


def _normalize_state(raw: str) -> str:
    s = raw.strip()
    if len(s) == 2:
        return s.upper()
    return _STATE_ABBR.get(s.lower(), s.upper())


def _extract_states(value: str) -> list[str]:
    return [_normalize_state(part) for part in value.split(",") if part.strip()]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def class_code_excluded(submission_class: str, excluded: tuple[str, ...]) -> str | None:
    """MM-04: explicit exclusion list, checked (and wins) before acceptance."""
    sc = _norm(submission_class)
    for exc in excluded:
        e = _norm(exc)
        if e == "roofing":
            if "roofing" in sc:
                return exc
        elif e == "contractors - all types":
            if sc.startswith("contractors"):
                return exc
        elif e in sc or sc in e:
            return exc
    return None


def class_code_accepted(submission_class: str, accepted: tuple[str, ...]) -> str | None:
    """MM-01: scoped semantic match — NOT plain string containment. Roofing scope
    (steep vs. low-slope) is the deliberately hard case (see the interpretation
    guide's MM-01 note)."""
    sc = _norm(submission_class)
    for acc in accepted:
        a = _norm(acc)
        if "roofing" in a and "roofing" in sc:
            if "low slope only" in a or "low-slope only" in a:
                low_slope_only = "low slope" in sc or "low-slope" in sc
                broader_scope = "steep" in sc or "all types" in sc
                if low_slope_only and not broader_scope:
                    return acc
                continue
            return acc  # bare "roofing" or "(all types, steep and low slope)" -> any roofing scope
        if a == sc or a in sc or sc in a:
            return acc
    return None


@dataclass
class HardExclusion:
    carrier_id: str
    carrier_name: str
    rule: str
    reason: str


@dataclass
class CarrierMatch:
    carrier_id: str
    carrier_name: str
    score: float
    class_fit_specificity: float
    completeness_score: float
    historical_hit_rate: float
    appetite_confidence_weight: float
    severity_margin: float
    missing: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _all_field_values(model: ExtractedModel, name: str) -> list[str]:
    return [str(f.value) for f in model.fields if f.name == name]


def _first_field(model: ExtractedModel, name: str) -> str | None:
    vals = _all_field_values(model, name)
    return vals[0] if vals else None


def _max_single_claim_incurred(model: ExtractedModel) -> float | None:
    """Severity must look at EVERY claim's `loss_run.incurred` line, not just the
    last one — the shared rules_engine's flatten-to-dict step would silently
    collapse repeated claim lines to the last value, which is exactly wrong here
    (this is computed natively for that reason, not routed through evaluate())."""
    raw_values = _all_field_values(model, "loss_run.incurred")
    values = [n for raw in raw_values if (n := _to_number(raw)) is not None]
    return max(values) if values else None


def _resolve_loss_run_years(model: ExtractedModel) -> int | None:
    """Tries, in order: the explicit field, the policy-period parenthetical, then
    any `total_incurred_Nyr`-shaped field name (datasets are inconsistent about
    which of these three they actually populate)."""
    explicit = _first_field(model, "loss_run.years_of_history_provided")
    if explicit is not None:
        m = re.search(r"\d+", explicit)
        if m:
            return int(m.group())

    period = _first_field(model, "loss_run.policy_period_covered")
    if period:
        m = _LOSS_RUN_YEARS_RE.search(period)
        if m:
            return int(m.group(1))

    for f in model.fields:
        if f.name.startswith("loss_run.total_incurred_"):
            m = re.search(r"(\d+)yr", f.name)
            if m:
                return int(m.group(1))
    return None


def _with_field(model: ExtractedModel, name: str, value: object) -> ExtractedModel:
    """Replaces (all occurrences of) a field by name with a single canonical
    value. Used to feed the SHARED rules_engine (and this module's own native
    logic — both read the same canonicalized model) a cleaned value instead of
    raw, annotation-laden extraction text, e.g. "$28,000 (per incumbent
    expiring premium)" -> 28000.0. A plain append would be ambiguous: the
    rules_engine's flatten-to-dict step keeps the LAST same-named field while
    `_first_field` here reads the FIRST — replacing avoids that mismatch."""
    kept = [f for f in model.fields if f.name != name]
    resolved = ExtractedValue(name=name, value=value)
    return ExtractedModel(submission_id=model.submission_id, fields=[*kept, resolved])


def _augment_model(
    model: ExtractedModel, loss_run_years: int | None, premium: float | None
) -> ExtractedModel:
    """Canonicalizes the two fields whose raw extraction text the shared
    rules_engine's naive numeric coercion can't reliably parse (see
    `_resolve_loss_run_years` and `_to_number`'s docstring)."""
    augmented = model
    years_field = "loss_run.years_of_history_provided"
    if loss_run_years is not None and _first_field(model, years_field) is None:
        augmented = _with_field(augmented, years_field, loss_run_years)
    if premium is not None:
        augmented = _with_field(augmented, "acord.indicated_premium_target", premium)
    return augmented


def _severity_is_hard(submission_class: str) -> bool:
    """MM-05's hard/soft distinction is per class, not universal (interpretation
    guide). None of this panel's carrier profiles carry an explicit
    ``ceiling_type`` field, so this implements the guide's own worked example
    directly: roofing severity ceilings are firm appetite boundaries; elsewhere
    severity is a scoring factor. See Validation_Rules_Test_Dataset.md's
    "Known dataset/guide discrepancy" note for the one case (submission_04)
    where this diverges from the interpretation guide's summary-table prose."""
    return "roofing" in _norm(submission_class)


def _diligent_search_flag(model: ExtractedModel) -> dict[str, object]:
    """MM-07 — independent of ranking, fires on every submission including
    zero-match. No per-state DS-requirement reference data was provided, so
    this conservatively assumes documentation is required everywhere (standard
    surplus-lines practice) and checks whether it's on file. A 3-declination
    threshold is this implementation's own reasonable default (not stated in
    the source docs) for "documented diligent search complete"."""
    declination_field = next((f for f in model.fields if "declination" in f.name), None)
    if declination_field is None:
        return {
            "required": True,
            "on_file": 0,
            "compliant": False,
            "note": "Diligent-search documentation required; none on file yet — "
            "needs to be generated/requested.",
        }
    m = re.search(r"\d+", str(declination_field.value))
    count = int(m.group()) if m else 0
    compliant = count >= 3
    note = (
        f"{count} admitted-market declination(s) on file — compliant."
        if compliant
        else f"{count} admitted-market declination(s) on file — "
        f"needs {3 - count} more for a complete diligent-search record."
    )
    return {"required": True, "on_file": count, "compliant": compliant, "note": note}


_WEIGHTS = {
    "class_fit_specificity": 0.30,
    "completeness_score": 0.25,
    "historical_hit_rate": 0.25,
    "appetite_confidence_weight": 0.10,
    "severity_margin": 0.10,
}


async def evaluate_carrier(
    session: AsyncSession,
    ctx: Ctx,
    engine: DefaultRulesEngine,
    profile: CarrierProfile,
    model: ExtractedModel,
) -> CarrierMatch | HardExclusion:
    """Runs MM-01..MM-06 for one carrier against one submission's extracted data.
    Returns a HardExclusion (tier 1) or a scored CarrierMatch (tiers 2+3)."""
    submission_class = _first_field(model, "acord.class_code") or ""
    states_value = _first_field(model, "acord.states_of_operation") or ""
    submission_states = _extract_states(states_value)
    loss_run_years = _resolve_loss_run_years(model)
    premium = _to_number(_first_field(model, "acord.indicated_premium_target") or "")
    augmented = _augment_model(model, loss_run_years, premium)

    # MM-04 (checked first — exclusions win on data conflict) + MM-01: native,
    # scope-aware class matching (not expressible as a generic field check).
    excl_reason = class_code_excluded(submission_class, profile.class_codes_excluded)
    if excl_reason is not None:
        return HardExclusion(profile.carrier_id, profile.carrier_name, "MM-04",
                              f"Class '{submission_class}' matches exclusion '{excl_reason}'")
    accepted_reason = class_code_accepted(submission_class, profile.class_codes_accepted)
    if accepted_reason is None:
        return HardExclusion(profile.carrier_id, profile.carrier_name, "MM-01",
                              f"Class '{submission_class}' not in accepted appetite")

    # MM-02: native — every state the submission operates in must be licensed
    # (list-subset logic, not one of the 6 generic check types).
    missing_states = [s for s in submission_states if s not in profile.states_licensed]
    if missing_states:
        return HardExclusion(profile.carrier_id, profile.carrier_name, "MM-02",
                              f"Not licensed in: {', '.join(missing_states)}")

    # MM-03 + MM-06 inputs: genuinely reused via the SHARED rules_engine, one
    # RuleSet per carrier (seed_rules.py), evaluated against the augmented model.
    results = await engine.evaluate(session, ctx, ruleset_key(profile.carrier_id), augmented)
    by_id = {r.rule_id: r for r in results}

    premium_ok = by_id["premium.min"].passed and by_id["premium.max"].passed
    if not premium_ok:
        band = profile.premium_band
        premium_str = _first_field(model, "acord.indicated_premium_target") or "?"
        band_str = f"${band.min:,.0f}-${band.max:,.0f}"
        return HardExclusion(
            profile.carrier_id, profile.carrier_name, "MM-03",
            f"Indicated premium {premium_str} outside band {band_str}",
        )

    flags: list[str] = []
    band = profile.premium_band
    edge = 0.10
    edge_note = f"final quote may fall outside {profile.carrier_name}'s appetite"
    if premium is not None:
        if premium <= band.min * (1 + edge):
            flags.append(f"Near {profile.carrier_name}'s premium floor — {edge_note}.")
        elif premium >= band.max * (1 - edge):
            flags.append(f"Near {profile.carrier_name}'s premium ceiling — {edge_note}.")

    completeness_results = [r for r in results if r.rule_id not in ("premium.min", "premium.max")]
    failed = [r for r in completeness_results if not r.passed]
    completeness_score = (
        (len(completeness_results) - len(failed)) / len(completeness_results)
        if completeness_results else 1.0
    )
    missing = [r.message or r.rule_id for r in failed]
    if missing:
        flags.append(f"Missing/incomplete: {', '.join(missing)}")

    # MM-05: severity ceiling — hard for roofing, soft (scored) elsewhere. Needs
    # the max across ALL claims, which the shared engine's flatten-to-dict step
    # can't give us (see `_max_single_claim_incurred`) — native by necessity.
    max_claim = _max_single_claim_incurred(model)
    ceiling = profile.severity_ceiling.max_single_claim_incurred
    if max_claim is not None and max_claim > ceiling and _severity_is_hard(submission_class):
        return HardExclusion(
            profile.carrier_id, profile.carrier_name, "MM-05",
            f"Single claim ${max_claim:,.0f} exceeds hard severity ceiling ${ceiling:,.0f}",
        )
    if max_claim is None:
        severity_margin = 1.0
    else:
        severity_margin = max(0.0, (ceiling - max_claim) / ceiling)
        if max_claim > ceiling:
            flags.append(
                f"Single claim ${max_claim:,.0f} exceeds severity ceiling ${ceiling:,.0f} "
                "(soft factor for this class)."
            )

    class_fit = 1.0 if _norm(accepted_reason) == _norm(submission_class) else 0.75
    confidence_weight = _CONFIDENCE_WEIGHT.get(profile.appetite_confidence, 0.6)

    score = (
        class_fit * _WEIGHTS["class_fit_specificity"]
        + completeness_score * _WEIGHTS["completeness_score"]
        + profile.historical_hit_rate_this_class * _WEIGHTS["historical_hit_rate"]
        + confidence_weight * _WEIGHTS["appetite_confidence_weight"]
        + severity_margin * _WEIGHTS["severity_margin"]
    )

    return CarrierMatch(
        carrier_id=profile.carrier_id,
        carrier_name=profile.carrier_name,
        score=round(score, 4),
        class_fit_specificity=class_fit,
        completeness_score=round(completeness_score, 4),
        historical_hit_rate=profile.historical_hit_rate_this_class,
        appetite_confidence_weight=confidence_weight,
        severity_margin=round(severity_margin, 4),
        missing=missing,
        flags=flags,
    )


def _to_number(value: str) -> float | None:
    """Extracts the leading number from a value, ignoring trailing free text
    (e.g. "$28,000 (per incumbent expiring premium)" -> 28000.0). Stripping
    currency symbols and parsing the WHOLE string, as the shared rules_engine's
    own numeric coercion does, fails on exactly this kind of annotated value —
    which is why premium/severity values are cleaned here before being handed
    to `engine.evaluate()` (see `_with_field` / `evaluate_carrier`)."""
    m = _NUMBER_RE.search(value.replace("$", "").replace("%", ""))
    if not m:
        return None
    cleaned = m.group().replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


async def rank_carriers(
    session: AsyncSession,
    ctx: Ctx,
    engine: DefaultRulesEngine,
    panel: list[CarrierProfile],
    model: ExtractedModel,
) -> tuple[list[CarrierMatch], list[HardExclusion]]:
    """Runs every carrier in the panel; returns (ranked matches desc by score,
    hard exclusions) — hard-excluded carriers NEVER appear in the ranked list,
    per the guide's tier-1 rule (not ranked last, not shown with a low score)."""
    matches: list[CarrierMatch] = []
    exclusions: list[HardExclusion] = []
    for profile in panel:
        result = await evaluate_carrier(session, ctx, engine, profile, model)
        if isinstance(result, HardExclusion):
            exclusions.append(result)
        else:
            matches.append(result)
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches, exclusions


async def decide_market_match(
    session: AsyncSession,
    ctx: Ctx,
    engine: DefaultRulesEngine,
    panel: list[CarrierProfile],
    model: ExtractedModel,
) -> Decision:
    """The E&S Decision Core entry point — normalizes ranking output into the
    shared, vertical-agnostic ``Decision`` DTO. Ranked matches ride in
    ``details['matches']`` per core/common's own documented convention for E&S;
    MM-07 rides in ``details['diligent_search']`` and fires regardless of
    whether any carrier matched."""
    matches, exclusions = await rank_carriers(session, ctx, engine, panel, model)
    diligent_search = _diligent_search_flag(model)

    outcome = DecisionOutcome.PROCEED if matches else DecisionOutcome.DECLINE
    rationale = (
        f"{len(matches)} carrier(s) matched on current panel."
        if matches
        else "No carrier on the current panel fits this submission — no market found."
    )

    return Decision(
        outcome=outcome,
        score=matches[0].score if matches else None,
        rationale=rationale,
        details={
            "matches": [
                {
                    "carrier_id": m.carrier_id,
                    "carrier_name": m.carrier_name,
                    "score": m.score,
                    "missing": m.missing,
                    "flags": m.flags,
                }
                for m in matches
            ],
            "excluded": [
                {
                    "carrier_id": e.carrier_id,
                    "carrier_name": e.carrier_name,
                    "rule": e.rule,
                    "reason": e.reason,
                }
                for e in exclusions
            ],
            "diligent_search": diligent_search,
        },
    )
