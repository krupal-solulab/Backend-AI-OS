"""Pydantic output schema for Renewal Remarketing — mirrors PRD §7.1's
Remarket Decision Output Schema closely. A few additive fields beyond the
literal schema (``pct_change``/``note`` on exposure change,
``remarketing_history_detail`` as a string rather than the schema's
structured list — the actual fixture data is prose, not structured
records — and the comparison-stage fields for RR-06) — same allowance
every prior workflow's schema.py took. Not a ``core.common`` contract;
free to evolve.
"""

from __future__ import annotations

from pydantic import BaseModel


class ExposureChangeOut(BaseModel):
    fields_changed: list[str] = []
    material: bool = False
    already_endorsed: bool = False
    pct_change: float = 0.0
    note: str | None = None


class LossHistoryChangeOut(BaseModel):
    new_claims_count: int = 0
    favorable_resolutions_count: int = 0
    trend: str = "flat"  # improving | worsening | flat


class IncumbentStatusOut(BaseModel):
    renewal_terms_received: bool = False
    days_before_expiration_at_receipt: int | None = None
    non_response_flag: bool = False


class ReasoningCitationOut(BaseModel):
    claim: str
    source: str


class TriggerReasoningOut(BaseModel):
    summary: str
    citations: list[ReasoningCitationOut] = []


class TriggerDecisionOut(BaseModel):
    # NO_REMARKET | LIGHT_REMARKET_CHECK | FULL_REMARKET | URGENT_REMARKET
    level: str = "NO_REMARKET"
    reasoning: TriggerReasoningOut


class ComparisonOptionOut(BaseModel):
    carrier_name: str
    premium: float | None = None
    limits: str | None = None
    deductible: float | None = None
    is_exception_based: bool = False
    exception_detail: str | None = None


class ComparisonOutputOut(BaseModel):
    directly_comparable: bool = True
    material_differences: list[str] = []
    incumbent: ComparisonOptionOut | None = None
    alternative: ComparisonOptionOut | None = None


class RemarketExecutionOut(BaseModel):
    initiated: bool = False
    market_matching_output_id: str | None = None
    comparison_output: ComparisonOutputOut | None = None


class FinalDecisionOut(BaseModel):
    outcome: str = "pending"  # renewed_incumbent | switched_carrier | pending
    decided_by: str | None = None
    timestamp: str | None = None


class RemarketDecisionPayload(BaseModel):
    renewal_review_id: str
    bind_id: str | None = None
    named_insured: str | None = None
    incumbent_carrier_id: str | None = None
    incumbent_carrier_name: str = ""
    is_comparison_stage: bool = False
    exposure_change: ExposureChangeOut = ExposureChangeOut()
    loss_history_change: LossHistoryChangeOut = LossHistoryChangeOut()
    incumbent_status: IncumbentStatusOut = IncumbentStatusOut()
    remarketing_history_detail: str | None = None
    trigger_decision: TriggerDecisionOut
    remarket_execution: RemarketExecutionOut = RemarketExecutionOut()
    final_decision: FinalDecisionOut = FinalDecisionOut()
