"""Pydantic output schema for Carrier Appetite Intelligence Tracking —
mirrors PRD §7's schema closely. Two deviations, both deliberate:

- ``reason_scope`` adds a third value, ``unstated``, beyond the literal
  schema's ``class_level | account_specific`` — an outcome with no
  captured reason (or an unrecognized one) is genuinely neither, and must
  not silently count as either (same "absence isn't exclusion" discipline
  applied throughout this vertical, e.g. Diligent Search's
  PENDING_DETERMINATION).
- ``metadata_refresh`` is additive: per the approved plan, no mutable
  Carrier Appetite Profile store exists anywhere in this codebase to
  write into (Market Matching's profiles are read-only JSON), so CI-03's
  refresh is COMPUTED and RECORDED here rather than applied to any real
  profile — a stated v1 limitation, not hidden. Populated only when
  ``pattern_type == "CONFIRMED_CONSISTENT"``.

Not a ``core.common`` contract; free to evolve.
"""

from __future__ import annotations

from pydantic import BaseModel


class EvidenceItemOut(BaseModel):
    submission_id: str
    outcome: str
    date: str
    stated_reason: str | None = None
    reason_scope: str | None = None  # class_level | account_specific | unstated | null


class MetadataRefreshOut(BaseModel):
    appetite_confidence: str
    appetite_last_updated: str


class CarrierAppetiteEvaluationPayload(BaseModel):
    suggestion_id: str
    carrier_id: str
    carrier_name: str = ""
    class_code: str
    evidence: list[EvidenceItemOut] = []
    pattern_type: str  # CONFIRMED_CONSISTENT | GENUINE_INCONSISTENCY | INSUFFICIENT_SIGNAL
    suggested_action: str | None = None  # null unless GENUINE_INCONSISTENCY
    # SUPPRESSED | METADATA_AUTO_UPDATED | PENDING_REVIEW | APPROVED | DISMISSED
    status: str = "SUPPRESSED"
    metadata_refresh: MetadataRefreshOut | None = None
