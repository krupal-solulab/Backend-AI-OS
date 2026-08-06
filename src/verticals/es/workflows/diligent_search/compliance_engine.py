"""Native DS-01..DS-05 logic. None of this fits the generic 6-check rules
engine — this is compound, sequential, per-state decision reasoning
(Option-A precedent, same as every prior E&S workflow's own engine module).

This is the highest legal-stakes workflow in the vertical (per the PRD's
own §8 risk register), so every function here leans on explicit,
evidence-grounded distinctions rather than convenient collapses:

- ``requirement_status`` is a genuine three-way split (REQUIRED / EXEMPT /
  PENDING_DETERMINATION), never defaulted from an absent state entry.
- An export-list exemption is only auto-resolved to EXEMPT when the note
  is UNCONDITIONAL (Scenario 02's Texas: "IS on the export list... not
  required"). A HEDGED, account-specific note (Scenario 04's Florida:
  "MAY be export-eligible... for large commercial accounts") is FR-7
  territory — flagged for human/legal review as PENDING_DETERMINATION,
  never auto-resolved just because ``export_list_class`` is technically
  ``true`` in the data.
- ``sufficiency_status`` distinguishes two genuinely different situations
  that would otherwise look identical: "evidence was gathered and falls
  short" (Scenario 03 — INSUFFICIENT, a confirmed compliance gap) versus
  "the state is REQUIRED but no declination evidence has been submitted
  for assessment at all yet" (Scenario 04's TN/GA — NOT_APPLICABLE, an
  open/incomplete state, not a confirmed failure). Collapsing these two
  would either wrongly call an untouched state "insufficient" (implying a
  compliance failure exists when it might not) or wrongly call a
  confirmed gap "not applicable" (hiding a real blocker).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_HEDGE_PATTERNS = [
    re.compile(r"\bmay\b", re.IGNORECASE),
    re.compile(r"\bdepends?\b", re.IGNORECASE),
    re.compile(r"\bfor large\b", re.IGNORECASE),
    re.compile(r"\bneeds? confirmation\b", re.IGNORECASE),
    re.compile(r"\bcan depend\b", re.IGNORECASE),
]


def _is_hedged_exemption(note: str | None) -> bool:
    """FR-7: hedged/account-specific export-list language must not
    auto-resolve to EXEMPT. See module docstring for the Scenario
    02-vs-04 evidence this distinguishes on."""
    if not note:
        return False
    return any(p.search(note) for p in _HEDGE_PATTERNS)


@dataclass(frozen=True)
class Declination:
    carrier: str
    date: str | None
    written_evidence: bool


@dataclass(frozen=True)
class StateDetermination:
    state: str
    requirement_status: str  # REQUIRED | EXEMPT | PENDING_DETERMINATION
    exemption_basis: str | None = None
    declinations_required: int | None = None
    declinations_on_file: list[Declination] = field(default_factory=list)
    sufficiency_status: str = "NOT_APPLICABLE"  # SUFFICIENT | INSUFFICIENT | NOT_APPLICABLE
    gap_detail: str | None = None
    document_eligible: bool = False  # True only when SUFFICIENT — DS-04's generation gate


def _parse_declinations(raw: list[dict[str, Any]] | None) -> list[Declination]:
    if not raw:
        return []
    return [
        Declination(
            carrier=str(d.get("carrier", "")),
            date=d.get("date"),
            written_evidence=bool(d.get("written_evidence", False)),
        )
        for d in raw
    ]


def determine_state(
    state: str,
    requirement: dict[str, Any] | None,
    declinations_raw: list[dict[str, Any]] | None,
) -> StateDetermination:
    """DS-01/DS-02/DS-03 for exactly one state. ``requirement is None``
    means this state has no entry at all in the submission's state
    reference data (FR-1/FR-6: an incomplete check must be marked
    pending, never silently assumed to match already-confirmed states)."""
    if requirement is None:
        return StateDetermination(
            state=state,
            requirement_status="PENDING_DETERMINATION",
            gap_detail="State requirement and export-list status not yet checked for this state.",
        )

    export_list_class = bool(requirement.get("export_list_class", False))
    note = requirement.get("export_list_note")

    if export_list_class and not _is_hedged_exemption(note):
        return StateDetermination(
            state=state,
            requirement_status="EXEMPT",
            exemption_basis=note or "Class code is on this state's export list.",
        )

    if export_list_class and _is_hedged_exemption(note):
        return StateDetermination(
            state=state,
            requirement_status="PENDING_DETERMINATION",
            declinations_required=requirement.get("admitted_declinations_required"),
            gap_detail=(
                f"Export-list eligibility is account-specific and unconfirmed: {note!r}. "
                "Flagged for human/legal review per FR-7 — not auto-resolved to exempt."
            ),
        )

    required_count = int(requirement.get("admitted_declinations_required", 0))

    if declinations_raw is None:
        return StateDetermination(
            state=state,
            requirement_status="REQUIRED",
            declinations_required=required_count,
            gap_detail=(
                "Requirement confirmed; declination evidence not yet submitted for assessment."
            ),
        )

    declinations = _parse_declinations(declinations_raw)
    written = [d for d in declinations if d.written_evidence]

    if len(written) >= required_count:
        return StateDetermination(
            state=state,
            requirement_status="REQUIRED",
            declinations_required=required_count,
            declinations_on_file=declinations,
            sufficiency_status="SUFFICIENT",
            document_eligible=True,
        )

    verbal_only = [d.carrier for d in declinations if not d.written_evidence]
    # "Additional" counts against declinations ON FILE (not written-only): a verbal
    # decline already occupies one of the required slots, it just needs upgrading —
    # distinct from needing an entirely fresh carrier approached. Matches the
    # dataset's own worked example (Scenario 03: 3 required, 2 on file (1 verbal) ->
    # "need 1 more decline... and upgrade the verbal one", not "need 2 more").
    additional_needed = max(required_count - len(declinations), 0)
    parts = []
    if additional_needed > 0:
        parts.append(f"need {additional_needed} more admitted decline(s) with written evidence")
    if verbal_only:
        parts.append(
            f"upgrade verbal-only decline(s) from {', '.join(verbal_only)} to written if possible"
        )
    detail = "; ".join(parts)
    return StateDetermination(
        state=state,
        requirement_status="REQUIRED",
        declinations_required=required_count,
        declinations_on_file=declinations,
        sufficiency_status="INSUFFICIENT",
        gap_detail=detail,
    )


def compute_overall_status(states: list[StateDetermination]) -> str:
    """FR-6: multi-state risks present a per-state checklist, never a
    single collapsed verdict hiding per-state nuance — but the schema's
    ``overall_status`` still needs one of 3 values. BLOCKED takes priority
    (a confirmed compliance gap, Scenario 03); else PARTIAL if any state
    is still genuinely open (Scenario 04); else COMPLETE (Scenarios 01/02,
    where every state is either exempt or evidenced)."""
    if any(s.sufficiency_status == "INSUFFICIENT" for s in states):
        return "BLOCKED"
    if any(
        s.requirement_status == "PENDING_DETERMINATION"
        or (s.requirement_status == "REQUIRED" and s.sufficiency_status == "NOT_APPLICABLE")
        for s in states
    ):
        return "PARTIAL"
    return "COMPLETE"
