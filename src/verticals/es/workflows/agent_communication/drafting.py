"""Native Option-A engine for Retail Agent Communication: deterministic trigger
classification, RA-TN tone/framing selection (see the companion
``TONE_FRAMING_RULES_GUIDE.md``), and the compliance-gate determination. None of
this fits the generic 6-check rules engine — it's compound routing logic, not a
data-driven check — so it lives natively here, thresholds as config, mirroring
package_assembly's ``assembly.py``.

Per PRD §8's architecture diagram, tone/framing selection is a deterministic,
rule-based routing step, NOT an LLM judgment call — the LLM (called from
service.py's ``draft()``) only turns already-decided facts + framing
instructions into prose. Subject lines are likewise generated natively here
(templated, not LLM free text) for every trigger type except
``NO_RESPONSE_FOLLOWUP``, whose subject depends on a prior draft's actual
subject line (FR-10) and is resolved by ``subject_resolver.py`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from core.common.dtos import ExtractedValue

KNOWN_TRIGGER_TYPES = {
    "SUBMISSION_ACKNOWLEDGMENT",
    "MISSING_INFO_REQUEST",
    "NO_MARKET_FOUND",
    "QUOTE_TERMS_SUMMARY",
    "PLACEMENT_CONFIRMATION",
    "NO_RESPONSE_FOLLOWUP",
    "POLICY_DOCUMENTS_DELIVERED",
    "ENDORSEMENT_CONFIRMED",
}

_TONE_INSTRUCTIONS: dict[str, str] = {
    "SUBMISSION_ACKNOWLEDGMENT": (
        "Draft a routine status update for the retail agent (RA-TN-02). Lead with "
        "the concrete action taken (which carriers were approached), factually "
        "note any already-disclosed carrier-specific gap without alarm, and set a "
        "clear expectation on response timing."
    ),
    "MISSING_INFO_REQUEST": (
        "Draft a request for missing information, scoped PRECISELY to the one "
        "carrier named in the facts (RA-TN-01) — do not imply any other carrier "
        "or submission is affected. Number the specific missing items. For any "
        "item that needs the insured's own input (not something the broker can "
        "supply), say so explicitly — never imply the system will guess it."
    ),
    "NO_MARKET_FOUND": (
        "Draft a market-search-outcome notice. Describe the review at the "
        "AGGREGATE carrier-panel level ONLY (RA-TN-06) — never name a specific "
        "carrier or how it individually responded. Explicitly separate this "
        "being a market-access constraint for the class from anything about the "
        "account's own quality (RA-TN-07) — frame as 'couldn't find a fit right "
        "now', not a heavier register. Offer a concrete next step."
    ),
    "QUOTE_TERMS_SUMMARY": (
        "Draft a quote/terms summary. If pricing context is provided, connect "
        "the price explicitly to the specific loss-history factor driving it "
        "(RA-TN-08) — never present a number with no cause. Only reframe the "
        "outcome relative to a scarce-market alternative (RA-TN-09) if that is "
        "actually stated in the facts. If the relationship is newer, be more "
        "explanatory and patient about market dynamics (RA-TN-10)."
    ),
    "PLACEMENT_CONFIRMATION": (
        "Draft a bind confirmation. Lead with the outcome immediately — this is "
        "unambiguous good news. Present the bound terms clearly for quick "
        "scanning, and note the next step (policy documents to follow)."
    ),
    "NO_RESPONSE_FOLLOWUP": (
        "Draft a follow-up on an earlier, still-unanswered request. Assume good "
        "faith (RA-TN-01/16) — never imply the agent ignored anything. Anchor "
        "urgency to the ACTUAL remaining time in the carrier's acceptance "
        "window given in the facts (RA-TN-05) — do not manufacture urgency "
        "beyond what that window genuinely implies."
    ),
    "POLICY_DOCUMENTS_DELIVERED": (
        "Draft a transaction-complete notice confirming the final policy "
        "documents have been verified and are attached/forwarded. This fires "
        "only once Binder & Policy Issuance has confirmed the issued policy "
        "matches the bound terms exactly (BI-05) — state plainly that the "
        "policy has been reviewed and matches what was bound, and note the "
        "next step (documents attached / forwarded separately)."
    ),
    "ENDORSEMENT_CONFIRMED": (
        "Draft a confirmation that the requested mid-term change has been "
        "verified and processed. This fires only once the issued endorsement "
        "has been reconciled item-by-item against what was actually "
        "requested (EP-05) — state plainly what was confirmed, referencing "
        "the specific change requested, and never imply anything was "
        "confirmed that wasn't actually verified."
    ),
}


@dataclass(frozen=True)
class PreparedCommunication:
    """Everything ``decide()``/``draft()``/``package()`` need, already decided by
    deterministic rules — the LLM only fills in prose from this."""

    trigger_type: str
    carrier_name: str | None
    subject_line: str
    tone_instructions: str
    facts: list[ExtractedValue] = field(default_factory=list)
    requires_compliance_review: bool = False
    carrier_names_disclosed: bool = False


def classify_trigger_type(data: dict[str, Any]) -> str:
    """FR-1/5.2: for v1, classification is reading the ``trigger_type`` the
    upstream workflow (or manual log entry) already stamped on the object —
    every one of the six types in the PRD's mapping table is keyed off a field
    already present in its source object (Package Assembly's status, Market
    Matching's zero_match_result, or a manual entry)."""
    trigger_type = data.get("trigger_type")
    if trigger_type not in KNOWN_TRIGGER_TYPES:
        raise ValueError(f"unknown or missing trigger_type: {trigger_type!r}")
    return str(trigger_type)


def carrier_name_for(data: dict[str, Any]) -> str | None:
    """Not every trigger names a single carrier (e.g. a multi-carrier
    acknowledgment) — returns None in that case rather than guessing one."""
    value = data.get("carrier_name")
    return str(value) if value else None


def requires_compliance_review(trigger_type: str) -> bool:
    """FR-8: only No Market Found is compliance-gated in v1 — RA-TN-06's
    carrier-name-disclosure question is unresolved, so every such draft is held
    until a human clears it (see subject_resolver.py's sibling, the router's
    ``compliance-clear`` endpoint)."""
    return trigger_type == "NO_MARKET_FOUND"


def carrier_names_disclosed(trigger_type: str) -> bool:
    """Every trigger type except No Market Found names its carrier(s) directly
    by design (per the PRD's own sample drafts) — No Market Found is the one
    type that must stay at the aggregate panel level (RA-TN-06)."""
    return trigger_type != "NO_MARKET_FOUND"


def _subject_line(trigger_type: str, data: dict[str, Any]) -> str:
    named_insured = data.get("named_insured") or "Submission"
    carrier = carrier_name_for(data)
    if trigger_type == "SUBMISSION_ACKNOWLEDGMENT":
        return f"{named_insured} - Submission Received, In Market"
    if trigger_type == "MISSING_INFO_REQUEST":
        return f"{named_insured} - Info Needed for {carrier}" if carrier else (
            f"{named_insured} - Info Needed"
        )
    if trigger_type == "NO_MARKET_FOUND":
        return f"{named_insured} - Market Search Update"
    if trigger_type == "QUOTE_TERMS_SUMMARY":
        return f"{named_insured} - Quote Received from {carrier}" if carrier else (
            f"{named_insured} - Quote Received"
        )
    if trigger_type == "PLACEMENT_CONFIRMATION":
        return f"{named_insured} - Bound with {carrier}" if carrier else f"{named_insured} - Bound"
    if trigger_type == "POLICY_DOCUMENTS_DELIVERED":
        return f"{named_insured} - Policy Documents"
    if trigger_type == "ENDORSEMENT_CONFIRMED":
        return f"{named_insured} - Endorsement Confirmed" if not carrier else (
            f"{named_insured} - Endorsement Confirmed with {carrier}"
        )
    raise ValueError(
        f"_subject_line has no template for {trigger_type!r} "
        "(NO_RESPONSE_FOLLOWUP is resolved via subject_resolver.py, not here)"
    )


def _fact(name: str, value: Any) -> ExtractedValue | None:
    if value in (None, "", [], {}):
        return None
    return ExtractedValue(name=name, value=value)


def build_facts(trigger_type: str, data: dict[str, Any]) -> list[ExtractedValue]:
    """Per-type whitelist of which trigger fields become grounded facts for the
    LLM (FR-6/RA-TN rules) — deliberately excludes anything that shouldn't leak
    for a given type (e.g. no carrier-level breakdown is ever passed for
    NO_MARKET_FOUND, consistent with RA-TN-06 — the source data for that type
    doesn't even carry one)."""
    facts: list[ExtractedValue] = []
    common = [
        _fact("named_insured", data.get("named_insured")),
        _fact("agent_relationship_tenure", data.get("agent_relationship_tenure")),
    ]

    if trigger_type == "SUBMISSION_ACKNOWLEDGMENT":
        specific = [
            _fact("carriers_approached", data.get("carriers_approached")),
            _fact("carrier_statuses", data.get("carrier_statuses")),
            _fact("expected_response_window_days", data.get("expected_response_window_days")),
        ]
    elif trigger_type == "MISSING_INFO_REQUEST":
        specific = [
            _fact("carrier_name", data.get("carrier_name")),
            _fact("blocking_items", data.get("blocking_items")),
        ]
    elif trigger_type == "NO_MARKET_FOUND":
        specific = [
            _fact("class_code", data.get("class_code")),
            _fact("carriers_reviewed_count", data.get("carriers_reviewed_count")),
            _fact("diligent_search", data.get("diligent_search")),
        ]
    elif trigger_type == "QUOTE_TERMS_SUMMARY":
        specific = [
            _fact("carrier_name", data.get("carrier_name")),
            _fact("quoted_terms", data.get("quoted_terms")),
            _fact("quote_context", data.get("quote_context")),
        ]
    elif trigger_type == "PLACEMENT_CONFIRMATION":
        specific = [
            _fact("carrier_name", data.get("carrier_name")),
            _fact("bound_terms", data.get("bound_terms")),
        ]
    elif trigger_type == "NO_RESPONSE_FOLLOWUP":
        specific = [
            _fact("carrier_name", data.get("carrier_name")),
            _fact("original_request_type", data.get("original_request_type")),
            _fact("days_since_original_request", data.get("days_since_original_request")),
            _fact("carrier_acceptance_window_days", data.get("carrier_acceptance_window_days")),
            _fact("days_remaining_in_window", data.get("days_remaining_in_window")),
        ]
    elif trigger_type == "POLICY_DOCUMENTS_DELIVERED":
        specific = [
            _fact("carrier_name", data.get("carrier_name")),
            _fact("binder_number", data.get("binder_number")),
            _fact("verified_terms", data.get("verified_terms")),
        ]
    elif trigger_type == "ENDORSEMENT_CONFIRMED":
        specific = [
            _fact("carrier_name", data.get("carrier_name")),
            _fact("endorsement_number", data.get("endorsement_number")),
            _fact("requested_change_detail", data.get("requested_change_detail")),
            _fact("issued_items", data.get("issued_items")),
        ]
    else:  # pragma: no cover - classify_trigger_type() already validated this
        specific = []

    facts.extend(f for f in common + specific if f is not None)
    return facts


def prepare(trigger_type: str, data: dict[str, Any]) -> PreparedCommunication:
    """Builds the full deterministic plan for one draft. For
    NO_RESPONSE_FOLLOWUP, ``subject_line`` is left empty — the caller
    (service.py's ``decide()``) fills it in via ``subject_resolver.py``, since
    that lookup needs DB access this pure function doesn't have."""
    subject = "" if trigger_type == "NO_RESPONSE_FOLLOWUP" else _subject_line(trigger_type, data)
    return PreparedCommunication(
        trigger_type=trigger_type,
        carrier_name=carrier_name_for(data),
        subject_line=subject,
        tone_instructions=_TONE_INSTRUCTIONS[trigger_type],
        facts=build_facts(trigger_type, data),
        requires_compliance_review=requires_compliance_review(trigger_type),
        carrier_names_disclosed=carrier_names_disclosed(trigger_type),
    )


def with_subject(prepared: PreparedCommunication, subject_line: str) -> PreparedCommunication:
    """``PreparedCommunication`` is frozen — this returns a copy with the
    resolved subject line filled in (used for NO_RESPONSE_FOLLOWUP only)."""
    return replace(prepared, subject_line=subject_line)
