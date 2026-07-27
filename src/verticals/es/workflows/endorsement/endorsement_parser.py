"""Native parsing helpers for this workflow's two genuinely new challenges:

1. Splitting a multi-part change request's free-text ``detail`` (e.g. "Add
   BOTH Midstate Distribution Co. AND Harborline Logistics as scheduled
   additional insureds") into a list of distinct requested items, for EP-05's
   item-level reconciliation.
2. Parsing the carrier's issued-endorsement confirmation email — same
   unstructured-email family as Binder & Issuance's bind confirmations, but
   a genuinely different shape ("Added as scheduled additional insured: X"
   lines, no premium/deductible focus) — independent of
   ``binder_issuance.bind_parser``'s internals (no cross-import), per the
   established Option-A precedent applied a fifth time.

Everything else this workflow needs (named_insured, carrier, the
structured requested_change type/detail) already arrives pre-extracted in
``bound_policy_context.json`` — there's no ACORD-style raw-document
extraction challenge here the way Quote Comparison/Binder & Issuance had
for their primary inputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

_HEADER_RE = re.compile(r"^(From|To|Subject|Date):\s*(.*)$", re.MULTILINE)
_ITEM_SPAN_RE = re.compile(
    r"[Aa]dd\s+(?:BOTH\s+)?(.+?)(?:\s+as\s+(?:scheduled\s+)?additional insureds?"
    r"|\s*,?\s*effective\b|\Z)",
)
_AND_SPLIT_RE = re.compile(r"\s+AND\s+", re.IGNORECASE)
_MMDDYYYY_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")
_ENDORSEMENT_NUMBER_RE = re.compile(r"Endorsement Number:\s*(\S+)")
_ISSUED_ITEM_RE = re.compile(
    r"Added as (?:scheduled )?additional insured:?\s*(.+?)(?:\n|$)", re.IGNORECASE
)
_EFFECTIVE_DATE_RE = re.compile(r"Effective Date:\s*([\d/]+)")
_NO_PREMIUM_RE = re.compile(r"no additional premium", re.IGNORECASE)


def split_requested_items(detail: str) -> list[str]:
    """Splits a multi-part additional-insured request into distinct items.
    Single-item requests return a one-element list unchanged. Does NOT
    strip trailing periods — a company name legitimately ending in an
    abbreviation ("... Co.") must keep it; only the outer regex's own
    stop-words are trimmed."""
    match = _ITEM_SPAN_RE.search(detail)
    span = match.group(1).strip() if match else detail.strip()
    return [p.strip() for p in _AND_SPLIT_RE.split(span) if p.strip()]


def extract_requested_effective_date(*texts: str) -> date | None:
    """Looks for an explicit MM/DD/YYYY date in the detail/email text —
    absent in several scenarios (e.g. "effective immediately," "starting
    next month"), which is fine; this is informational, not blocking."""
    for text in texts:
        match = _MMDDYYYY_RE.search(text)
        if match:
            mm, dd, yyyy = match.group(1).split("/")
            return date(int(yyyy), int(mm), int(dd))
    return None


@dataclass(frozen=True)
class ParsedIssuedEndorsement:
    carrier_name: str
    endorsement_number: str | None
    issued_items: list[str] = field(default_factory=list)
    effective_date: date | None = None
    no_additional_premium: bool = False


_CARRIER_DOMAINS: dict[str, str] = {
    "ironcladcasualty.com": "Ironclad Casualty Solutions",
    "meridianexcess.com": "Meridian Excess & Surplus",
    "palmettospecialty.com": "Palmetto Specialty Underwriters",
    "coastalmutualspecialty.com": "Coastal Mutual Specialty",
    "harborspecialtyproperty.com": "Harbor Specialty Property",
    "vantageexcess.com": "Vantage Excess Partners",
    "apexexcess.com": "Apex Excess Lines",
}


def _carrier_name_from_domain(from_line: str) -> str:
    match = re.search(r"@([\w.-]+)", from_line)
    domain = match.group(1).lower() if match else ""
    if domain in _CARRIER_DOMAINS:
        return _CARRIER_DOMAINS[domain]
    stem = domain.split(".")[0] if domain else "Unknown Carrier"
    return stem.replace("-", " ").title()


def parse_date_mmddyyyy(value: str | None) -> date | None:
    if not value:
        return None
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    if not match:
        return None
    mm, dd, yyyy = match.groups()
    return date(int(yyyy), int(mm), int(dd))


def parse_issued_endorsement(raw_text: str) -> ParsedIssuedEndorsement:
    header = dict(_HEADER_RE.findall(raw_text))
    body = _HEADER_RE.sub("", raw_text).strip()
    carrier_name = _carrier_name_from_domain(header.get("From", ""))

    number_match = _ENDORSEMENT_NUMBER_RE.search(body)
    effective_match = _EFFECTIVE_DATE_RE.search(body)
    issued_items = [
        item.strip()
        for line_match in _ISSUED_ITEM_RE.finditer(body)
        for item in _AND_SPLIT_RE.split(line_match.group(1).strip())
        if item.strip()
    ]

    return ParsedIssuedEndorsement(
        carrier_name=carrier_name,
        endorsement_number=number_match.group(1) if number_match else None,
        issued_items=issued_items,
        effective_date=parse_date_mmddyyyy(effective_match.group(1)) if effective_match else None,
        no_additional_premium=bool(_NO_PREMIUM_RE.search(body)),
    )
