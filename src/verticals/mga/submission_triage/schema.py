"""Pydantic output schemas for MGA Submission Triage.

Field names are camelCase to map 1:1 onto the MGA-FE ``TriageDetail`` / ``Submission``
shapes (mocks.ts). These are workflow-local response models — the frozen ``core/common``
contracts are unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TriageDoc(BaseModel):
    name: str
    kind: str
    pages: int
    fields: int
    confidence: float
    classified: bool


class ExtractedFieldOut(BaseModel):
    key: str
    label: str
    value: str | None
    required: bool
    confidence: float
    source: str | None = None


class ConsistencyCheck(BaseModel):
    label: str
    detail: str
    status: str  # ok | warn | fail


class MissingItem(BaseModel):
    item: str
    reason: str
    severity: str  # required | recommended


class RiskFactor(BaseModel):
    name: str
    value: str
    weight: int


class AppetiteResultOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rule: str
    passed: bool = Field(serialization_alias="pass", validation_alias="pass")
    hard: bool
    detail: str


class ActivityEntry(BaseModel):
    at: str
    who: str
    what: str
    ctx: str | None = None
    conf: str | None = None


class LossMetrics(BaseModel):
    totalIncurred: str
    totalPaid: str
    openClaims: int
    years: int
    required: int
    trend: str  # improving | worsening | flat


class TriageMeta(BaseModel):
    received: list[str]
    lowConfidence: list[str]
    timestamp: str


class TriageDetail(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recommendation: str
    confidence: float
    hardRulePassed: bool
    failedRules: list[str]
    processing: str
    rulesVersion: str
    meta: TriageMeta
    docs: list[TriageDoc]
    fields: list[ExtractedFieldOut]
    loss: LossMetrics
    consistency: list[ConsistencyCheck]
    missingInfo: list[MissingItem]
    factors: list[RiskFactor]
    narrative: str
    citations: list[str]
    appetite: list[AppetiteResultOut]
    activity: list[ActivityEntry]


class SubmissionRow(BaseModel):
    """Inbox/list row — the MGA-FE ``Submission`` shape."""

    id: str
    insured: str
    industry: str
    state: str
    tiv: str
    premium: str
    score: int | None
    appetite: str
    recommendation: str
    status: str
    received: str


class ActRequest(BaseModel):
    action: str  # approve | send | escalate
    amount: float | None = None
    note: str | None = None
