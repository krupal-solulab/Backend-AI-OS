"""Native parser for carrier-response emails -> structured quote/declination
fields (PRD §7.1's Extracted Quote Schema).

NOT ``core.extraction.DefaultExtractionService`` — a documented, deliberate
Option-A choice (see the approved plan): that service parses ``Label: value``
strictly PER LINE, which breaks on this dataset's own most important field —
subjectivity clauses that wrap across lines (e.g. "...operations prior\\nto
binding") would get silently truncated exactly where QC-02's grounding
matters most. Declinations also carry no Key:Value structure at all (pure
prose), and single lines like "Deductible: $10,000 (all perils), Wind/Hail:
$100,000" pack two distinct values the shared extractor never splits. None of
this fits the shared, generic extractor, so it's native here — same
precedent as package_assembly's ``assembly.py``.

All classification below (materiality, endorsement basis, declination
consistency) is a documented HEURISTIC over this dataset's phrasing, not true
NLU — flagged the same way every other native rule in this project is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# Known carrier email domains -> display name (every carrier appearing in
# this dataset; a novel domain falls back to a naive derived name rather than
# crashing — see `_carrier_name_from_domain`).
_CARRIER_DOMAINS: dict[str, str] = {
    "ironcladcasualty.com": "Ironclad Casualty Solutions",
    "meridianexcess.com": "Meridian Excess & Surplus",
    "palmettospecialty.com": "Palmetto Specialty Underwriters",
    "coastalmutualspecialty.com": "Coastal Mutual Specialty",
    "harborspecialtyproperty.com": "Harbor Specialty Property",
    "vantageexcess.com": "Vantage Excess",
    "apexexcess.com": "Apex Excess Lines",
}

_HEADER_RE = re.compile(r"^(From|To|Subject|Date):\s*(.*)$", re.MULTILINE)
_PREMIUM_RE = re.compile(r"Premium:\s*\$?([\d,]+)")
_LIMITS_RE = re.compile(
    r"(General Liability|Property|Excess Casualty):\s*(.+?)(?:\n|$)"
)
_DEDUCTIBLE_RE = re.compile(r"Deductible:\s*(.+?)(?:\n|$)")
_EFFECTIVE_DATE_RE = re.compile(r"Effective Date:\s*([\d/]+)")
_VALID_THROUGH_RE = re.compile(r"Quote valid through:\s*([\d/]+)")
_SUBJECT_TO_RE = re.compile(
    r"Subject to:\s*(.+?)(?=\n\s*(?:Quote valid through|Effective Date)\s*:|\Z)",
    re.DOTALL,
)
_ENDORSEMENT_RE = re.compile(
    r"(Additional Insured|Waiver of [Ss]ubrogation)\s*(?:endorsement)?:\s*(.+?)(?:\n|$)"
)
_NUMBERED_ITEM_RE = re.compile(r"\(\d+\)\s*")
_DOLLAR_RE = re.compile(r"\$([\d,]+)")
_DAYS_RE = re.compile(r"within\s+(\d+)\s+days?", re.IGNORECASE)

# Declination trigger phrasing (FR: declination reason extraction) — the
# dataset's own wording ("not able to offer terms", "decline", "unable to
# offer") plus common real-world carrier decline phrasing, so genuine carrier
# emails outside this fixture dataset's exact wording still extract a
# reason instead of silently falling through to "unable_to_determine".
_DECLINATION_TRIGGER_RE = re.compile(
    r"not able to offer terms|unable to offer|unable to quote|unable to provide|"
    r"unable to write|not able to provide|not able to quote|"
    r"regret(?:s|fully)?\s+(?:to inform|that)|will not be able to|"
    r"cannot offer|cannot provide|elect(?:ing)?\s+not to offer|"
    r"outside\s+(?:of\s+)?our appetite|does not (?:fall|meet) within our appetite|"
    r"pass on this (?:risk|submission|opportunity|account)|"
    r"unable to (?:move forward|proceed)|\bdecline\b",
    re.IGNORECASE,
)
_DECLINATION_STOP_RE = re.compile(
    r"^(Happy to|Please|Thank you|We appreciate|Feel free|Should you|"
    r"If you have|We look forward)\b"
)

# Materiality (QC-02): a subjectivity is MATERIAL iff it carries its own
# countdown/deadline, an actionable scheduling verb, an unresolved dependency
# on another party/policy resolving, or a requirement for additional
# underwriting information (per RULE_ENGINE_INTERPRETATION_GUIDE.md's own
# examples) — otherwise ROUTINE.
_MATERIAL_PATTERNS = [
    re.compile(r"within\s+\d+\s+days?", re.IGNORECASE),
    re.compile(r"must be (scheduled|completed|provided)", re.IGNORECASE),
    re.compile(r"binding confirmed", re.IGNORECASE),
    re.compile(
        r"remaining .*(loss (run|history)|financials|questionnaire|information)", re.IGNORECASE
    ),
    re.compile(r"\bpending\b", re.IGNORECASE),
]

# A dependency specifically on another action/party resolving (FR-16) — a
# narrower subset of "material" that gets its own `dependency_unresolved`
# urgency-flag type, distinct from a generic material subjectivity.
_DEPENDENCY_PATTERNS = [
    re.compile(r"binding confirmed", re.IGNORECASE),
]


@dataclass(frozen=True)
class Deductibles:
    all_perils: str | None = None
    wind_hail: str | None = None


@dataclass(frozen=True)
class Endorsement:
    type: str
    basis: str  # included_blanket | scheduled_only | additional_premium | not_offered


@dataclass(frozen=True)
class Subjectivity:
    description: str
    materiality: str  # routine | material
    is_dependency: bool
    deadline_or_dependency: str | None = None


@dataclass(frozen=True)
class ParsedResponse:
    filename: str
    carrier_name: str
    named_insured: str | None
    response_date: date | None
    response_type: str  # QUOTE | DECLINATION
    premium: float | None = None
    limits: str | None = None
    deductibles: Deductibles | None = None
    key_endorsements: list[Endorsement] = field(default_factory=list)
    subjectivities: list[Subjectivity] = field(default_factory=list)
    effective_date: str | None = None
    quote_valid_through: str | None = None
    declination_reason: str | None = None
    declination_reason_amount: float | None = None


def _carrier_name_from_domain(from_line: str, body: str = "") -> str:
    match = re.search(r"@([\w.-]+)", from_line)
    domain = match.group(1).lower() if match else ""
    if domain in _CARRIER_DOMAINS:
        return _CARRIER_DOMAINS[domain]
    # Domain isn't a recognized carrier (e.g. a shared test inbox used to
    # simulate several different carriers) — look for one of the known
    # carrier names spelled out in the email's own text (signature block,
    # letterhead) before falling back to a domain-derived guess.
    lowered_body = body.lower()
    for name in _CARRIER_DOMAINS.values():
        if name.lower() in lowered_body:
            return name
    stem = domain.split(".")[0] if domain else "Unknown Carrier"
    return stem.replace("-", " ").title()


def _named_insured_from_subject(subject: str) -> str | None:
    text = re.sub(r"^RE:\s*", "", subject.strip(), flags=re.IGNORECASE)
    return text.split(" - ")[0].strip() or None


def _parse_date(text: str) -> date | None:
    # "Tue, 27 Jul 2027 10:15:00 -0500" -> just need month/day/year.
    match = re.search(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", text)
    if not match:
        return None
    day, mon, year = match.groups()
    months = {
        "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    }
    if mon not in months:
        return None
    return date(int(year), months[mon], int(day))


def _parse_mmddyyyy(text: str) -> date | None:
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not match:
        return None
    mm, dd, yyyy = match.groups()
    return date(int(yyyy), int(mm), int(dd))


def _classify_endorsement_basis(text: str) -> str:
    low = text.lower()
    if "not offered" in low:
        return "not_offered"
    if "additional premium" in low:
        return "additional_premium"
    if "scheduled" in low or "per-project" in low or "per project" in low:
        return "scheduled_only"
    if "blanket" in low or "included" in low:
        return "included_blanket"
    return "not_offered"


def _split_subjectivity_items(blob: str) -> list[str]:
    joined = " ".join(line.strip() for line in blob.splitlines() if line.strip())
    if _NUMBERED_ITEM_RE.search(joined):
        parts = _NUMBERED_ITEM_RE.split(joined)
        return [p.strip().rstrip(",").strip() for p in parts if p.strip()]
    return [p.strip() for p in joined.split(",") if p.strip()]


def _classify_subjectivity(item: str) -> Subjectivity:
    is_material = any(p.search(item) for p in _MATERIAL_PATTERNS)
    is_dependency = any(p.search(item) for p in _DEPENDENCY_PATTERNS)
    deadline_match = _DAYS_RE.search(item)
    deadline = deadline_match.group(0) if deadline_match else (item if is_dependency else None)
    return Subjectivity(
        description=item,
        materiality="material" if is_material else "routine",
        is_dependency=is_dependency,
        deadline_or_dependency=deadline if is_material else None,
    )


_ALL_PERILS_DED_RE = re.compile(r"\$?([\d,]+)\s*\(all perils\)", re.IGNORECASE)
_WIND_HAIL_DED_RE = re.compile(r"wind/?hail:?\s*\$?([\d,]+)", re.IGNORECASE)


def _parse_deductibles(raw: str) -> Deductibles:
    # NOT a naive comma-split — "$25,000 (all perils), Wind/Hail: $100,000"
    # has a thousands-separator comma INSIDE the first value, which would
    # corrupt a plain ``raw.split(",", 1)``. Extract each labeled figure
    # directly instead.
    if "wind" in raw.lower():
        all_match = _ALL_PERILS_DED_RE.search(raw)
        wind_match = _WIND_HAIL_DED_RE.search(raw)
        return Deductibles(
            all_perils=f"${all_match.group(1)}" if all_match else None,
            wind_hail=f"${wind_match.group(1)}" if wind_match else None,
        )
    return Deductibles(all_perils=raw.strip(), wind_hail=None)


def _extract_declination_reason(body: str) -> tuple[str | None, float | None]:
    """Captures the sentence(s) explaining a decline, stopping before a
    trailing pleasantry ("Happy to look again...") — a documented heuristic,
    not general-purpose sentence parsing."""
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(body.split()))
    reason_sentences: list[str] = []
    capturing = False
    for sentence in sentences:
        if _DECLINATION_TRIGGER_RE.search(sentence):
            capturing = True
            continue
        if capturing:
            if _DECLINATION_STOP_RE.match(sentence.strip()):
                break
            reason_sentences.append(sentence.strip())
    reason = " ".join(reason_sentences).strip() or None
    amount = None
    if reason:
        dollar_match = _DOLLAR_RE.search(reason)
        if dollar_match:
            amount = float(dollar_match.group(1).replace(",", ""))
    return reason, amount


def parse_response(filename: str, raw_text: str) -> ParsedResponse:
    header = dict(_HEADER_RE.findall(raw_text))
    body = _HEADER_RE.sub("", raw_text).strip()
    carrier_name = _carrier_name_from_domain(header.get("From", ""), body)
    named_insured = _named_insured_from_subject(header.get("Subject", ""))
    response_date = _parse_date(header.get("Date", ""))

    if not _PREMIUM_RE.search(body):
        reason, amount = _extract_declination_reason(body)
        return ParsedResponse(
            filename=filename,
            carrier_name=carrier_name,
            named_insured=named_insured,
            response_date=response_date,
            response_type="DECLINATION",
            declination_reason=reason,
            declination_reason_amount=amount,
        )

    premium_match = _PREMIUM_RE.search(body)
    premium = float(premium_match.group(1).replace(",", "")) if premium_match else None

    limits_match = _LIMITS_RE.search(body)
    limits = f"{limits_match.group(1)}: {limits_match.group(2)}".strip() if limits_match else None

    deductibles = None
    ded_match = _DEDUCTIBLE_RE.search(body)
    if ded_match:
        deductibles = _parse_deductibles(ded_match.group(1))

    endorsements = [
        Endorsement(type=etype, basis=_classify_endorsement_basis(ebasis))
        for etype, ebasis in _ENDORSEMENT_RE.findall(body)
    ]

    subj_match = _SUBJECT_TO_RE.search(body)
    subjectivities = (
        [_classify_subjectivity(item) for item in _split_subjectivity_items(subj_match.group(1))]
        if subj_match
        else []
    )

    effective_match = _EFFECTIVE_DATE_RE.search(body)
    valid_through_match = _VALID_THROUGH_RE.search(body)

    return ParsedResponse(
        filename=filename,
        carrier_name=carrier_name,
        named_insured=named_insured,
        response_date=response_date,
        response_type="QUOTE",
        premium=premium,
        limits=limits,
        deductibles=deductibles,
        key_endorsements=endorsements,
        subjectivities=subjectivities,
        effective_date=effective_match.group(1) if effective_match else None,
        quote_valid_through=valid_through_match.group(1) if valid_through_match else None,
    )


def parse_valid_through_date(value: str | None) -> date | None:
    return _parse_mmddyyyy(value) if value else None
