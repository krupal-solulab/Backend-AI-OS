"""Pydantic output schema for Diligent Search & Compliance Documentation —
mirrors PRD §7's Compliance Record schema closely. One additive field
beyond the literal schema: ``generated_document_text`` per state (the
literal schema only has ``document_generated: boolean``, which gives the
FE nothing to actually show) — LLM-drafted, grounded only in that state's
own ``declinations_on_file``, populated only when ``document_generated``
is True (DS-04's gate). Not a ``core.common`` contract; free to evolve.
"""

from __future__ import annotations

from pydantic import BaseModel


class DeclinationOut(BaseModel):
    carrier: str
    date: str | None = None
    written_evidence: bool = False


class StateDeterminationOut(BaseModel):
    state: str
    requirement_status: str  # REQUIRED | EXEMPT | PENDING_DETERMINATION
    exemption_basis: str | None = None
    declinations_required: int | None = None
    declinations_on_file: list[DeclinationOut] = []
    sufficiency_status: str = "NOT_APPLICABLE"  # SUFFICIENT | INSUFFICIENT | NOT_APPLICABLE
    gap_detail: str | None = None
    document_generated: bool = False
    generated_document_text: str | None = None
    retention_period_years: int | None = None  # null: not yet sourced (FR-8) — v1 never guesses


class ComplianceRecordPayload(BaseModel):
    compliance_record_id: str
    submission_id: str | None = None
    named_insured: str | None = None
    state_determinations: list[StateDeterminationOut]
    overall_status: str  # COMPLETE | PARTIAL | BLOCKED
