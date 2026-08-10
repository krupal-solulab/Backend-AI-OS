"""Pydantic output schemas for MGA Portfolio & Book Performance Reporting.

Shapes follow the PRD's Section 7 data schema directly (camelCase for the FE, same
convention as every other workflow's schema.py).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FunnelStageOut(BaseModel):
    stage: str
    count: int
    pctOfPriorStage: float | None = None


class LossRatioOut(BaseModel):
    periodBasis: str
    earnedPremium: float
    incurredLosses: float
    ratioPct: float
    lowVolumeFlag: bool
    singleEventDrivenFlag: bool
    detail: str


class RenewalRetentionOut(BaseModel):
    eligible: int
    retained: int
    nonRenewedUnderwritingDecision: int
    lapsedNoDecision: int
    retentionRatePct: float
    lineItems: list[dict[str, str]] = Field(default_factory=list)


class GapOut(BaseModel):
    sourceWorkflow: str
    dateRange: str
    reason: str
    crossReferencedFindingId: str | None = None


class DataCompletenessOut(BaseModel):
    status: str  # COMPLETE | PARTIAL
    gaps: list[GapOut] = Field(default_factory=list)


class BrokerProductionOut(BaseModel):
    brokerAgency: str
    currentPeriodPremium: float
    priorPeriodPremium: float
    pctChange: float
    significantDecline: bool
    detail: str


class AppetiteExposureSectionOut(BaseModel):
    pulledFrom: str
    findingId: str
    summary: str
    lowVolumeFlag: bool


class ActivityEntry(BaseModel):
    at: str
    who: str
    what: str
    ctx: str | None = None
    conf: str | None = None


class PortfolioReportDetail(BaseModel):
    reportId: str
    period: str
    dataCompleteness: DataCompletenessOut
    funnel: list[FunnelStageOut] = Field(default_factory=list)
    lossRatio: LossRatioOut | None = None
    renewalRetention: RenewalRetentionOut | None = None
    brokerProduction: list[BrokerProductionOut] = Field(default_factory=list)
    appetiteExposureSection: AppetiteExposureSectionOut | None = None
    status: str  # READY | FLAGGED | PARTIAL
    rationale: str
    activity: list[ActivityEntry] = Field(default_factory=list)


class PortfolioReportRow(BaseModel):
    """Inbox/list row for the Portfolio & Book Performance Reporting screen."""

    id: str
    period: str
    status: str


class ActRequest(BaseModel):
    action: str  # approve | send | escalate
