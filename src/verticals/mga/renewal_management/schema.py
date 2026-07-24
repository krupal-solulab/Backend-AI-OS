"""Pydantic output schemas for MGA Renewal Management.

Shapes are LOCKED to the MGA-FE RenewalManagement types (camelCase JSON). The frozen
``core/common`` contracts are unchanged — this maps the frozen ``Decision`` onto the FE
``RenewalRecommendation`` here, in the workflow layer.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CompareRow(BaseModel):
    label: str
    prior: str
    current: str
    change: str | None = None
    direction: str | None = None  # favorable | unfavorable | neutral
    strong: bool | None = None


class ChangeFlag(BaseModel):
    category: str  # exposure | loss | appetite | timing | info
    label: str
    detail: str
    direction: str  # favorable | unfavorable | neutral


class LossChange(BaseModel):
    type: str  # new_claim | status_change | favorable_closure | trend
    description: str
    direction: str
    source: str | None = None


class RenewalChangeItem(BaseModel):
    item: str
    reason: str
    source: str | None = None


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


class RenewalTiming(BaseModel):
    daysToExpiration: int
    lapseRisk: bool
    noSubmission: bool


class RenewalBroker(BaseModel):
    name: str
    agency: str
    tenure: str
    note: str


class RenewalDetail(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recommendation: str  # RENEW_AS_IS | RENEW_WITH_CHANGES | NON_RENEW
    confidence: float
    processing: str  # queued | extracting | ready | error
    priorSource: str  # PAS | stored_triage_record | manual_queue
    rulesVersion: str
    rulesVersionAtBinding: str
    hardRulePassed: bool
    appetite: list[AppetiteResultOut]
    appetiteDrift: str | None = None
    comparison: list[CompareRow]
    changeFlags: list[ChangeFlag]
    lossChanges: list[LossChange]
    timing: RenewalTiming
    changes: list[RenewalChangeItem]
    narrative: str
    citations: list[str]
    broker: RenewalBroker
    activity: list[ActivityEntry]
    # workflow extras (not in the locked FE type, additive): needsInfo/missingInfo surface
    # the REQUEST_INFO state the RenewalRecommendation enum can't express.
    needsInfo: bool = False
    missingInfo: list[RenewalChangeItem] = Field(default_factory=list)
    retention: str = "neutral"
    # premium / loss-ratio (additive): priorPremium from prior_policy.expiring_premium;
    # indicated + premiumChange are GAPs ("—") until the workflow computes a re-rate.
    priorPremium: str = "—"
    indicated: str = "—"
    premiumChange: str = "—"
    lossRatio: str = "—"


class RenewalRow(BaseModel):
    """Inbox/list row for the RenewalManagement screen."""

    id: str
    insured: str
    recommendation: str
    score: int | None
    retention: str
    daysToExpiration: int
    lapseRisk: bool
    status: str
    received: str
    # premium / loss-ratio surfaced for the FE `Renewal` list shape (GAP → "—")
    priorPremium: str = "—"
    indicated: str = "—"
    change: str = "—"
    lossRatio: str = "—"


class ActRequest(BaseModel):
    action: str  # approve | send | escalate
    amount: float | None = None
    note: str | None = None
