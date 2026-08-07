"""Endorsement classification config — DATA, not code (mirrors ``AppetiteConfig`` /
``RenewalConfig``). MEP-01's type-based-first classification: administrative changes are
essentially always routine; these change types are never purely routine regardless of
apparent size, per the PRD's FR-1 and the reused Wholesale/E&S EP-01 principle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EndorsementConfig:
    # change request "type" values that always require a binding-authority check
    # (MEP-01) — never purely routine, regardless of apparent size.
    material_change_types: frozenset[str] = frozenset({
        "limit_increase", "location_addition", "add_operations_class",
    })
    # administrative change types considered essentially always routine
    routine_change_types: frozenset[str] = frozenset({
        "additional_insured", "employee_count_update", "contact_update",
    })
