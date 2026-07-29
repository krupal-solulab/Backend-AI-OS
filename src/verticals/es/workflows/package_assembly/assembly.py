"""Package Assembly engine — PA-01..PA-07 (see this workflow's
RULE_ENGINE_INTERPRETATION_GUIDE.md in Workflow_11's test dataset).

Native Python, not routed through core.rules_engine (Option-A pattern, as in
Market Matching's decision_core): every rule here is compound/cross-field
(document-family fuzzy matching, loss-run-year arithmetic, the auto-fill
grounding boundary, three-state status derivation) and doesn't reduce to the
generic evaluator's 6 flat field checks. Nothing in this workflow fits the
shared engine cleanly, so it's deliberately unused here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.common.dtos import Citation, ExtractedModel, ExtractedValue
from core.common.enums import DocumentKind

# ── PA-03 gap-vs-block policy (v1 placeholder, see Validation_Rules_Test_Dataset.md) ──
# Global default: block everything. One evidenced override: Vantage (CAR-06)
# treats a missing NON-STANDARD document type as disclose, not block.
_DEFAULT_POLICY = "block"
_CARRIER_POLICY_OVERRIDES: dict[str, dict[str, str]] = {
    "CAR-06": {"missing_document_type": "disclose"},
}

# Carrier supplemental-form field names that don't exactly match this
# service's auto-generated extraction key (see extraction.service's
# `_normalize_key`) — a small, explicit, hand-maintained alias list, NOT a
# fuzzy/inferential matcher. Anything not listed here (e.g.
# "unit_count_estimate_from_TIV_and_class") simply has no alias and falls
# through to "no direct source" by construction, which is the correct,
# safe-by-default behavior per PA-02.
_FIELD_NAME_ALIASES: dict[str, str] = {
    "construction_type": "construction",
}

_DOC_KIND_PREFIXES = tuple(k.value for k in DocumentKind)


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _extract_years(s: str) -> int | None:
    m = re.search(r"(\d+)[\s-]*year", s)
    return int(m.group(1)) if m else None


def _is_loss_run(s: str) -> bool:
    return "loss run" in s


def _is_financials(s: str) -> bool:
    return "financ" in s


def _is_sov(s: str) -> bool:
    return "statement of values" in s or re.search(r"\bsov\b", s) is not None


@dataclass
class DocChecklistItem:
    document_type: str
    included: bool
    source: str | None = None


@dataclass
class SupplementalField:
    field_name: str
    value: str | None
    auto_filled: bool
    source_citation: str | None = None


@dataclass
class BlockingItem:
    item: str
    reason: str


@dataclass
class GapItem:
    item: str
    cover_letter_acknowledgment: bool = True


@dataclass
class PackageResult:
    status: str  # READY | READY_WITH_GAP | BLOCKED
    document_checklist: list[DocChecklistItem] = field(default_factory=list)
    supplemental_form_fields: list[SupplementalField] = field(default_factory=list)
    diligent_search_attached: bool = False
    blocking_items: list[BlockingItem] = field(default_factory=list)
    gap_items_disclosed: list[GapItem] = field(default_factory=list)


def _known_issue_note(requirement: str, missing_info: list[dict[str, Any]]) -> str | None:
    """PA-01 reuses Market Matching's ALREADY-COMPUTED missing-info notes
    (FR-1: don't recompute what upstream already found) — checked before
    falling back to inferring presence/absence from the plain available-docs
    list. Returns the note text if this requirement was already flagged
    upstream, else None."""
    req_norm = _normalize(requirement)
    is_questionnaire = "questionnaire" in req_norm
    keyword = None
    if _is_loss_run(req_norm):
        keyword = "loss run"
    elif is_questionnaire:
        keyword = "questionnaire"
    if keyword is None:
        return None
    for entry in missing_info:
        if keyword in _normalize(str(entry.get("item", ""))):
            return str(entry["item"])
    return None


def check_document(
    requirement: str, available: list[str], missing_info: list[dict[str, Any]]
) -> tuple[bool, str | None]:
    """Returns (present, note). `note` explains a shortfall/absence when not present."""
    known = _known_issue_note(requirement, missing_info)
    req_norm = _normalize(requirement)

    if _is_loss_run(req_norm):
        req_years = _extract_years(req_norm)
        for a in available:
            a_norm = _normalize(a)
            if _is_loss_run(a_norm):
                avail_years = _extract_years(a_norm)
                if req_years is None or avail_years is None or avail_years >= req_years:
                    return True, None
                return False, f"only {avail_years} of {req_years} required years"
        if known:
            return False, known
        return False, "not provided"

    if _is_financials(req_norm):
        for a in available:
            if _is_financials(_normalize(a)):
                return True, None
        return False, known or "not provided"

    if _is_sov(req_norm):
        for a in available:
            if _is_sov(_normalize(a)):
                return True, None
        return False, known or "not provided"

    for a in available:
        if _normalize(a) == req_norm:
            return True, None
    if known:
        return False, known
    return False, "not provided"


def _classify_requirement_type(requirement: str, note: str | None) -> str:
    req_norm = _normalize(requirement)
    if note and "of" in note and "year" in note:
        return "loss_run_year_shortfall"
    if "questionnaire" in req_norm or "supplemental" in req_norm:
        return "supplemental_form_incomplete"
    return "missing_document_type"


def _resolve_policy(carrier_id: str, requirement_type: str) -> str:
    return _CARRIER_POLICY_OVERRIDES.get(carrier_id, {}).get(requirement_type, _DEFAULT_POLICY)


def _format_citation(citation: Citation | None) -> str | None:
    if citation is None:
        return None
    return f"{citation.filename}#{citation.locator}" if citation.locator else citation.filename


def _try_direct_extract(
    field_name: str, model: ExtractedModel
) -> tuple[str | None, Citation | None]:
    """PA-02's hard boundary: a value is only usable if it has a DIRECT,
    cited source among already-extracted fields. Tries the field name as
    given, then a small alias list, checking both flat `<kind>.<field>`
    fields and (verified against the real extraction service's output for
    multi-location SOVs) nested `<kind>.locations[i].<field>` entries —
    never computes, estimates, or infers a value from anything else."""
    candidates = [field_name, _FIELD_NAME_ALIASES.get(field_name, field_name)]
    for candidate in candidates:
        for prefix in _DOC_KIND_PREFIXES:
            flat_name = f"{prefix}.{candidate}"
            match = next((f for f in model.fields if f.name == flat_name), None)
            if match is not None and match.value not in (None, ""):
                return str(match.value), match.citation

            locations_name = f"{prefix}.locations"
            locations_field = next((f for f in model.fields if f.name == locations_name), None)
            is_list = isinstance(locations_field, ExtractedValue) and isinstance(
                locations_field.value, list
            )
            if is_list and locations_field is not None:
                for location in locations_field.value:
                    if isinstance(location, dict) and location.get(candidate) not in (None, ""):
                        return str(location[candidate]), locations_field.citation
    return None, None


def _form_version_disclosure_gaps(carrier_requirements: dict[str, Any]) -> list[GapItem]:
    """PA-05: package documents must conform to each carrier's stated
    format/version preferences. No sample fixture populates
    `preferred_form_versions`, and — more fundamentally — extraction never
    captures a document's version/edition at all (only its type, e.g.
    "ACORD 125"), so a real pass/fail compliance verdict can't be computed
    without fabricating data on one side or the other. Honest behavior: when
    a carrier DOES declare a preferred version, disclose it as an explicit
    open question via the existing gap mechanism (never silently ignored,
    never asserted as compliant/non-compliant) so a broker knows to confirm
    it manually before sending."""
    preferred_versions: dict[str, str] = carrier_requirements.get("preferred_form_versions") or {}
    return [
        GapItem(
            item=f"{form_key}: carrier prefers {preferred_version}",
            cover_letter_acknowledgment=False,
        )
        for form_key, preferred_version in preferred_versions.items()
    ]


def assemble_package(
    carrier_view: dict[str, Any],
    submission_extracted: ExtractedModel,
    *,
    document_check_fn: Callable[
        [str, list[str], list[dict[str, Any]]], tuple[bool, str | None]
    ] = check_document,
) -> PackageResult:
    """The PA-01..PA-06 engine for ONE carrier. PA-07 (precedence ordering)
    is architectural, not exercised here — see the module's docstring and
    Validation_Rules_Test_Dataset.md.

    ``document_check_fn`` defaults to the fixture path's exact-string/
    year-aware ``check_document`` above — every existing call site keeps
    byte-identical behavior. The real Market Matching -> Package Assembly
    live-ingestion path (``live_ingestion.py``) passes its own, coarser
    type-only completeness check instead, since real document extraction
    can't confirm an exact form edition or a loss run's covered years — see
    that module's docstring for exactly why."""
    carrier_id = carrier_view["carrier_id"]
    carrier_requirements = carrier_view.get("carrier_requirements", {})
    requirements: list[str] = carrier_requirements.get("required_documents", [])
    available: list[str] = carrier_view.get("documents_available_from_extraction") or []
    missing_info: list[dict[str, Any]] = carrier_view.get("missing_info_from_market_matching") or []

    checklist: list[DocChecklistItem] = []
    blocking: list[BlockingItem] = []
    gaps: list[GapItem] = _form_version_disclosure_gaps(carrier_requirements)

    for requirement in requirements:
        present, note = document_check_fn(requirement, available, missing_info)
        checklist.append(
            DocChecklistItem(
                document_type=requirement, included=present, source=requirement if present else None
            )
        )
        if not present:
            req_type = _classify_requirement_type(requirement, note)
            policy = _resolve_policy(carrier_id, req_type)
            if policy == "block":
                blocking.append(BlockingItem(item=requirement, reason=note or "not provided"))
            else:
                gaps.append(GapItem(item=requirement))

    supplemental_fields: list[SupplementalField] = []
    # NOTE: the scenario JSON nests these under carrier_requirements, not at
    # the carrier_view's top level (verified against the real fixture, not
    # assumed from the PRD's schema sketch alone).
    supplemental_form = carrier_requirements.get("supplemental_form") or carrier_view.get(
        "supplemental_form"
    )
    if supplemental_form:
        claimed_fillable: list[str] = carrier_requirements.get(
            "supplemental_fields_auto_fillable"
        ) or carrier_view.get("supplemental_fields_auto_fillable", [])
        for field_name in claimed_fillable:
            value, citation = _try_direct_extract(field_name, submission_extracted)
            supplemental_fields.append(
                SupplementalField(
                    field_name=field_name,
                    value=value,
                    auto_filled=value is not None,
                    source_citation=_format_citation(citation),
                )
            )

    diligent_search_attached = False
    diligent_search = carrier_view.get("diligent_search")
    if diligent_search:
        if diligent_search.get("documentation_status") == "present":
            diligent_search_attached = True
        elif diligent_search.get("required"):
            blocking.append(
                BlockingItem(
                    item="Diligent search documentation",
                    reason="required but not confirmed present",
                )
            )

    if blocking:
        status = "BLOCKED"
    elif gaps:
        status = "READY_WITH_GAP"
    else:
        status = "READY"

    return PackageResult(
        status=status,
        document_checklist=checklist,
        supplemental_form_fields=supplemental_fields,
        diligent_search_attached=diligent_search_attached,
        blocking_items=blocking,
        gap_items_disclosed=gaps,
    )
