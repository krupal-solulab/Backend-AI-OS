"""Native parsers for the two NEW extraction targets this workflow
introduces: carrier bind confirmation emails and issued-policy declarations
pages.

NOT the shared ``core.extraction.DefaultExtractionService``, and NOT an
import of ``quote_comparison.quote_parser``'s internals — a deliberate,
evidenced choice (see the approved plan): bind confirmations drop quote
comparison's validity-window/subjectivity-list shape and add a
``Binder Number:`` line, while the issued-policy declarations page is a
THIRD, structurally distinct format with no email headers at all. A shared
implementation wouldn't fully unify these anyway, so each lives natively
here, independent of quote_comparison's own parser (Option-A precedent,
applied a third time).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# Known carrier email domains -> display name (rebuilt natively here rather
# than importing quote_comparison's map — same known carriers, independent
# copy per the confirmed no-cross-import decision).
_CARRIER_DOMAINS: dict[str, str] = {
    "ironcladcasualty.com": "Ironclad Casualty Solutions",
    "meridianexcess.com": "Meridian Excess & Surplus",
    "palmettospecialty.com": "Palmetto Specialty Underwriters",
    "coastalmutualspecialty.com": "Coastal Mutual Specialty",
    "harborspecialtyproperty.com": "Harbor Specialty Property",
    "vantageexcess.com": "Vantage Excess Partners",
    "apexexcess.com": "Apex Excess Lines",
}

_HEADER_RE = re.compile(r"^(From|To|Subject|Date):\s*(.*)$", re.MULTILINE)
_BINDER_NUMBER_RE = re.compile(r"Binder Number:\s*(\S+)")
_PREMIUM_RE = re.compile(r"Premium:\s*\$?([\d,]+)")
_DEDUCTIBLE_RE = re.compile(r"Deductible:\s*\$?([\d,]+)")
_EFFECTIVE_DATE_RE = re.compile(r"Effective Date:\s*([\d/]+)")
_LIMITS_LINE_RE = re.compile(
    r"(General Liability|Property|Excess Casualty):\s*(.+?)(?:\n|$)"
)
_TIMELINE_RE = re.compile(r"within\s+(\d+)(?:-(\d+))?\s*days", re.IGNORECASE)
_MONEY_RE = re.compile(r"\d[\d,]*")


@dataclass(frozen=True)
class ParsedBindConfirmation:
    carrier_name: str
    binder_number: str | None
    premium: float | None
    limits_display: str | None
    deductible_all_perils: float | None
    deductible_wind_hail: float | None
    effective_date: date | None
    stated_issuance_timeline_days: int | None
    confirmation_date: date | None  # the email's own Date: header — BI-04's
    # "elapsed time SINCE BIND CONFIRMATION" reference (FR-10), distinct from
    # effective_date (when coverage attaches)


@dataclass(frozen=True)
class ParsedIssuedPolicy:
    named_insured: str | None
    policy_number: str | None
    premium: float | None
    limits_display: str | None
    deductible_all_perils: float | None
    deductible_wind_hail: float | None
    effective_date: date | None


def _carrier_name_from_domain(from_line: str) -> str:
    match = re.search(r"@([\w.-]+)", from_line)
    domain = match.group(1).lower() if match else ""
    if domain in _CARRIER_DOMAINS:
        return _CARRIER_DOMAINS[domain]
    stem = domain.split(".")[0] if domain else "Unknown Carrier"
    return stem.replace("-", " ").title()


def parse_money(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    match = _MONEY_RE.search(value)
    return float(match.group(0).replace(",", "")) if match else None


_EMAIL_DATE_RE = re.compile(r"(\d{1,2})\s+(\w{3})\s+(\d{4})")
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def parse_email_header_date(value: str) -> date | None:
    """Parses an RFC-2822-ish email ``Date:`` header, e.g.
    "Fri, 30 Jul 2027 16:00:00 -0500" — just needs day/month/year."""
    match = _EMAIL_DATE_RE.search(value)
    if not match:
        return None
    day, mon, year = match.groups()
    if mon not in _MONTHS:
        return None
    return date(int(year), _MONTHS[mon], int(day))


def parse_date_any(value: str | None) -> date | None:
    """Accepts ISO ("2027-09-01") or MM/DD/YYYY ("09/03/2027") — this
    workflow's terms arrive in BOTH formats (structured JSON vs. parsed
    email/declarations text) and must be compared as real dates, never as
    strings, or a same-date-different-format pair would falsely mismatch."""
    if not value:
        return None
    iso_match = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if iso_match:
        y, m, d = iso_match.groups()
        return date(int(y), int(m), int(d))
    slash_match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    if slash_match:
        mm, dd, yyyy = slash_match.groups()
        return date(int(yyyy), int(mm), int(dd))
    return None


def limits_signature(text: str | None) -> tuple[str, ...]:
    """Compares limits by the NUMBERS they contain, not exact phrasing —
    "General Liability $1,000,000/$2,000,000 aggregate" and "General
    Liability: $1,000,000/$2,000,000 aggregate" must reconcile as equal, and
    a property+GL split representation must reconcile against a single
    descriptive line covering the same figures."""
    if not text:
        return ()
    return tuple(m.replace(",", "") for m in _MONEY_RE.findall(text))


def parse_bind_confirmation(raw_text: str) -> ParsedBindConfirmation:
    header = dict(_HEADER_RE.findall(raw_text))
    body = _HEADER_RE.sub("", raw_text).strip()
    carrier_name = _carrier_name_from_domain(header.get("From", ""))

    binder_match = _BINDER_NUMBER_RE.search(body)
    premium_match = _PREMIUM_RE.search(body)
    deductible_match = _DEDUCTIBLE_RE.search(body)
    effective_match = _EFFECTIVE_DATE_RE.search(body)
    limits_match = _LIMITS_LINE_RE.search(body)
    timeline_match = _TIMELINE_RE.search(body)

    timeline_days = None
    if timeline_match:
        low, high = timeline_match.groups()
        timeline_days = int(high) if high else int(low)

    return ParsedBindConfirmation(
        carrier_name=carrier_name,
        binder_number=binder_match.group(1) if binder_match else None,
        premium=parse_money(premium_match.group(1)) if premium_match else None,
        limits_display=(
            f"{limits_match.group(1)}: {limits_match.group(2)}" if limits_match else None
        ),
        deductible_all_perils=parse_money(deductible_match.group(1)) if deductible_match else None,
        deductible_wind_hail=None,  # this dataset's bind confirmations never split wind/hail
        effective_date=parse_date_any(effective_match.group(1)) if effective_match else None,
        stated_issuance_timeline_days=timeline_days,
        confirmation_date=(
            parse_email_header_date(header["Date"]) if "Date" in header else None
        ),
    )


_NAMED_INSURED_RE = re.compile(r"Named Insured:\s*(.+)")
_POLICY_NUMBER_RE = re.compile(r"Policy Number:\s*(\S+)")
_POLICY_PREMIUM_RE = re.compile(r"^Premium:\s*\$?([\d,]+)", re.MULTILINE)
_PROPERTY_LIMIT_RE = re.compile(r"Property Limit:\s*\$?([\d,]+)")
_GL_LIMITS_RE = re.compile(r"General Liability Limits:\s*(.+)")
_ALL_PERILS_RE = re.compile(r"All Perils:\s*\$?([\d,]+)")
_WIND_HAIL_RE = re.compile(r"(?:Windstorm or Hail|Wind/?Hail):\s*\$?([\d,]+)", re.IGNORECASE)
_POLICY_EFFECTIVE_DATE_RE = re.compile(r"Effective Date:\s*([\d/]+)")


def parse_issued_policy(raw_text: str) -> ParsedIssuedPolicy:
    """Parses a declarations-page dump — NOT an email (no From/To/Subject/Date
    headers at all), a genuinely different shape from the bind-confirmation
    parser above."""
    named_insured_match = _NAMED_INSURED_RE.search(raw_text)
    policy_number_match = _POLICY_NUMBER_RE.search(raw_text)
    premium_match = _POLICY_PREMIUM_RE.search(raw_text)
    property_match = _PROPERTY_LIMIT_RE.search(raw_text)
    gl_match = _GL_LIMITS_RE.search(raw_text)
    all_perils_match = _ALL_PERILS_RE.search(raw_text)
    wind_hail_match = _WIND_HAIL_RE.search(raw_text)
    effective_match = _POLICY_EFFECTIVE_DATE_RE.search(raw_text)

    limits_parts = []
    if property_match:
        limits_parts.append(f"Property ${property_match.group(1)}")
    if gl_match:
        limits_parts.append(f"GL {gl_match.group(1).strip()}")

    return ParsedIssuedPolicy(
        named_insured=named_insured_match.group(1).strip() if named_insured_match else None,
        policy_number=policy_number_match.group(1) if policy_number_match else None,
        premium=parse_money(premium_match.group(1)) if premium_match else None,
        limits_display="; ".join(limits_parts) or None,
        deductible_all_perils=(
            parse_money(all_perils_match.group(1)) if all_perils_match else None
        ),
        deductible_wind_hail=(
            parse_money(wind_hail_match.group(1)) if wind_hail_match else None
        ),
        effective_date=parse_date_any(effective_match.group(1)) if effective_match else None,
    )
