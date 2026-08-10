"""Pydantic output schemas for MGA Appetite Governance & Audit Trail.

Shapes follow the PRD's Section 7 data schema directly (camelCase for the FE, same
convention as every other workflow's schema.py).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DecisionRecordOut(BaseModel):
    workflow: str
    recordId: str
    decision: str
    rulesVersionApplied: str
    underwriter: str


class GapOut(BaseModel):
    workflow: str
    dateRange: str
    reason: str


class DecisionTrailOut(BaseModel):
    status: str  # COMPLETE | PARTIAL
    gaps: list[GapOut] = Field(default_factory=list)
    decisions: list[DecisionRecordOut] = Field(default_factory=list)


class RuleVersionDriftFindingOut(BaseModel):
    policyNumber: str
    insured: str
    boundUnderVersion: str
    currentVersion: str
    stillQualifies: bool
    detail: str


class OverridePatternFindingOut(BaseModel):
    underwriter: str
    overrideCount: int
    undocumentedCount: int
    flagged: bool
    suggestion: str | None = None


class PortfolioConcentrationFindingOut(BaseModel):
    classCode: str
    carrier: str
    accountsNearCeiling: int
    totalAccountsInSegment: int
    lowVolumeFlag: bool
    detail: str


class GovernanceSuggestionOut(BaseModel):
    findingId: str
    findingType: str  # RULE_DRIFT | OVERRIDE_PATTERN | PORTFOLIO_CONCENTRATION
    status: str  # PENDING_REVIEW | REVIEWED | ACTIONED_EXTERNALLY
    summary: str


class AuditReportOut(BaseModel):
    """AG-05: external (carrier/regulatory) audit report — populated only for a
    CARRIER_DELEGATED_AUTHORITY_AUDIT request; None otherwise."""

    reportType: str
    carrierName: str
    period: str
    triageDecisions: int
    renewalDecisions: int
    bindDecisions: int
    endorsementDecisions: int
    authorityCeilingBreachesReferred: int
    authorityCeilingBreachesApproved: int
    authorityCeilingBreachesDeclined: int
    groundingStatement: str


class ActivityEntry(BaseModel):
    at: str
    who: str
    what: str
    ctx: str | None = None
    conf: str | None = None


class GovernanceDetail(BaseModel):
    auditPeriodId: str
    period: str
    decisionTrail: DecisionTrailOut
    ruleVersionDriftFindings: list[RuleVersionDriftFindingOut] = Field(default_factory=list)
    overridePatternFindings: list[OverridePatternFindingOut] = Field(default_factory=list)
    portfolioConcentrationFindings: list[PortfolioConcentrationFindingOut] = Field(
        default_factory=list)
    governanceSuggestionQueue: list[GovernanceSuggestionOut] = Field(default_factory=list)
    auditReport: AuditReportOut | None = None
    status: str  # COMPLETE_CLEAN | FLAGGED | SUGGESTED | READY | PARTIAL | INFORMATIONAL
    rationale: str
    activity: list[ActivityEntry] = Field(default_factory=list)


class GovernanceRow(BaseModel):
    """Inbox/list row for the Appetite Governance screen."""

    id: str
    period: str
    status: str
    findingCount: int


class ActRequest(BaseModel):
    action: str  # approve | send | escalate
