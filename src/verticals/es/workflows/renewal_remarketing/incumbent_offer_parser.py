"""Native parser for the ONE new extraction target this workflow's live path
introduces: the incumbent carrier's renewal-offer email (RR-03/RR-06).

The fixture dataset never modeled this as a raw-email extraction challenge
(``renewal_context.json``'s ``incumbent_renewal_offer`` always arrives as
already-structured JSON) — but a real incumbent renewal offer is exactly the
same "Premium:/Limits:/Deductible:/Effective Date:" free-text carrier email
family already parsed elsewhere in this vertical (bind confirmations, carrier
quotes). Kept independent here, NOT an import of
``binder_issuance.bind_parser``'s internals — same no-cross-import-for-
domain-parsing precedent already applied a few times over (quote_parser vs
bind_parser vs endorsement_parser), even though the shape rhymes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_PREMIUM_RE = re.compile(r"Premium:\s*\$?([\d,]+)")
_DEDUCTIBLE_RE = re.compile(r"Deductible:\s*\$?([\d,]+)")
_EFFECTIVE_DATE_RE = re.compile(r"Effective Date:\s*([\d/]+)")
_LIMITS_LINE_RE = re.compile(r"Limits:\s*(.+?)(?:\n|$)")
_MONEY_RE = re.compile(r"\d[\d,]*")


@dataclass(frozen=True)
class ParsedIncumbentOffer:
    premium: float | None
    limits: str | None
    deductible: float | None
    effective_date: str | None  # MM/DD/YYYY as stated, not reformatted


def _parse_money(value: str | None) -> float | None:
    if value is None:
        return None
    match = _MONEY_RE.search(value)
    return float(match.group(0).replace(",", "")) if match else None


def parse_incumbent_renewal_offer(raw_text: str) -> ParsedIncumbentOffer:
    """Parses a real incumbent renewal-offer email into exactly the dict
    shape ``remarket_engine.compare_renewal_options`` already expects
    (``premium``/``limits``/``deductible``) — no header/carrier-domain
    parsing needed here (unlike bind confirmations), since RR-06's
    comparison never reads the incumbent's own carrier name from this text
    — it's already known from the real bind record."""
    premium_match = _PREMIUM_RE.search(raw_text)
    deductible_match = _DEDUCTIBLE_RE.search(raw_text)
    effective_match = _EFFECTIVE_DATE_RE.search(raw_text)
    limits_match = _LIMITS_LINE_RE.search(raw_text)

    return ParsedIncumbentOffer(
        premium=_parse_money(premium_match.group(1)) if premium_match else None,
        limits=limits_match.group(1).strip() if limits_match else None,
        deductible=_parse_money(deductible_match.group(1)) if deductible_match else None,
        effective_date=effective_match.group(1) if effective_match else None,
    )
