"""Pydantic output schema for Retail Agent Communication — shapes what rides in
``OutputPackage.payload``. Mirrors PRD §7.1's Draft Communication Output Schema
almost exactly; ``carrier_name`` is an additive field beyond that schema (needed
for correct per-carrier scoping/dedup, per FR-4/FR-5 — see router.py), same
allowance package_assembly's own schema.py took. Not a ``core.common``
contract; free to evolve with this workflow's FE screen.
"""

from __future__ import annotations

from pydantic import BaseModel


class GroundingCitationOut(BaseModel):
    """A lighter-weight grounding note than the frozen ``Citation`` DTO (which
    requires a document kind/filename) — this workflow's facts come from an
    upstream structured object, not a scanned document, so citations point at
    the source FIELD instead."""

    claim: str
    source_field: str


class DraftCommunicationOut(BaseModel):
    """The FE review-queue screen's data needs for one drafted communication."""

    draft_id: str
    trigger_type: str
    source_workflow: str
    source_record_id: str | None = None
    submission_id: str | None = None
    named_insured: str | None = None
    carrier_name: str | None = None
    retail_agent_name: str | None = None
    retail_agency: str | None = None
    subject_line: str
    body: str
    requires_compliance_review: bool = False
    carrier_names_disclosed: bool = False
    grounding_citations: list[GroundingCitationOut] = []
    status: str = "DRAFT"  # DRAFT | UNDER_COMPLIANCE_REVIEW | APPROVED | SENT | DISCARDED
    edit_distance_from_original: float | None = None
    generated_timestamp: str
    sent_timestamp: str | None = None
