"""Pydantic output schema for Package Assembly — shapes what rides in
``OutputPackage.payload`` (the frozen contract's vertical-specific escape
hatch). Mirrors PRD Section 7.2's Package Output Schema. Not a
``core.common`` contract; free to evolve with this workflow's FE screen.
"""

from __future__ import annotations

from pydantic import BaseModel


class DocChecklistItemOut(BaseModel):
    document_type: str
    included: bool
    source: str | None = None


class SupplementalFieldOut(BaseModel):
    field_name: str
    value: str | None = None
    auto_filled: bool = False
    source_citation: str | None = None


class BlockingItemOut(BaseModel):
    item: str
    reason: str


class GapItemOut(BaseModel):
    item: str
    cover_letter_acknowledgment: bool = True


class CoverLetterCitationOut(BaseModel):
    claim: str
    source: str


class CoverLetterOut(BaseModel):
    body: str
    citations: list[CoverLetterCitationOut] = []


class StatusLogEntryOut(BaseModel):
    action: str  # generated | edited | approved | marked_sent
    timestamp: str
    user: str


class PackageAssemblyPayload(BaseModel):
    """The FE Package Assembly screen's data needs — one per carrier package,
    even within a multi-carrier simultaneous selection (FR-18)."""

    package_id: str
    submission_id: str | None
    carrier_id: str
    carrier_name: str
    status: str  # READY | READY_WITH_GAP | BLOCKED
    document_checklist: list[DocChecklistItemOut] = []
    supplemental_form_fields: list[SupplementalFieldOut] = []
    diligent_search_attached: bool = False
    cover_letter: CoverLetterOut
    blocking_items: list[BlockingItemOut] = []
    gap_items_disclosed: list[GapItemOut] = []
    status_log: list[StatusLogEntryOut] = []
