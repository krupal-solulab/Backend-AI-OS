"""Pydantic output schemas for MGA Broker Communication Copilot.

Shapes are LOCKED to the MGA-FE ``CommDraft`` type (camelCase JSON, mocks.ts). The
frozen ``core/common`` contracts are unchanged — this maps the frozen ``Decision`` (read
back off a Triage/Renewal ``OutputPackage``) onto the FE draft shape here, in the
workflow layer, same convention as ``renewal_management/schema.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BrokerContext(BaseModel):
    name: str
    agency: str
    email: str
    tenureYears: int | None = None
    volumeTier: str | None = None  # low | moderate | high | strategic


class CommCitation(BaseModel):
    claim: str
    source: str


class ActivityEntry(BaseModel):
    at: str
    who: str
    what: str
    ctx: str | None = None
    conf: str | None = None


class CommDraft(BaseModel):
    """One drafted broker communication — the FE ``communicationQueue`` row shape."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    type: str  # CommType (MISSING_INFO_REQUEST | CONSISTENCY_FLAG_FOLLOWUP | ...)
    sourceWorkflow: str  # "Submission Triage" | "Renewal Management"
    sourceId: str
    sourceRoute: str
    namedInsured: str
    broker: BrokerContext
    subject: str
    tone: str
    toneWhy: str
    sensitive: bool
    requiresComplianceReview: bool
    combined: str | None = None
    deadlineRef: str | None = None
    citations: list[CommCitation] = Field(default_factory=list)
    body: str
    status: str  # DRAFT | UNDER_COMPLIANCE_REVIEW | APPROVED | SENT | DISCARDED
    generatedAt: str
    activity: list[ActivityEntry] = Field(default_factory=list)


class ActRequest(BaseModel):
    action: str  # approve | send | escalate
    body: str | None = None  # human edit, applied before send
    note: str | None = None


class DraftRequest(BaseModel):
    """Trigger a draft from an existing Triage/Renewal decision."""

    source_workflow: str  # "submission-triage" | "renewal-management"
    submission_id: str
