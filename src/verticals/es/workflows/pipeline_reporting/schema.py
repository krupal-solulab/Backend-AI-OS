"""Pydantic output schema for Pipeline & Carrier Performance Reporting —
mirrors PRD §7's schema closely. Deviations, all deliberate:

- ``remarketing_value[].outcome_type`` adds a third value,
  ``not_remarketed``, beyond the literal schema's
  ``savings_identified | confirmation_value`` — an account that was never
  actually shopped is genuinely neither, same "add a real third state"
  discipline used throughout this vertical (``PENDING_DETERMINATION``,
  ``unstated`` reason_scope, etc.).
- ``funnel[].count``/``pct_of_prior_stage`` are nullable — a data-gap
  stage (PR-06) has neither, by design.
- ``overall_conversion_pct`` and ``carrier_performance[].overall_hit_rate``
  are additive (not in the literal §7 schema) — both appear in this
  dataset's own expected report output, so they're included for FE
  parity; the funnel's overall figure is withheld whenever any gap
  exists (see ``reporting_engine.build_funnel``).
- No revenue-attribution field anywhere (PR-04) — explicitly out of
  scope per the PRD ("do not build this rule from assumption").

Not a ``core.common`` contract; free to evolve.
"""

from __future__ import annotations

from pydantic import BaseModel


class DataGapOut(BaseModel):
    stage: str
    reason: str


class DataCompletenessOut(BaseModel):
    status: str  # COMPLETE | PARTIAL
    gaps: list[DataGapOut] = []


class FunnelStageOut(BaseModel):
    stage: str
    count: int | None = None
    pct_of_prior_stage: float | None = None


class CarrierPerformanceOut(BaseModel):
    carrier_name: str
    submissions_approached: int
    quote_rate: float
    bind_rate: float
    overall_hit_rate: float
    low_volume_flag: bool = False


class RemarketOutcomeOut(BaseModel):
    account: str
    trigger_level: str
    outcome_type: str  # savings_identified | confirmation_value | not_remarketed
    savings_amount: float | None = None
    note: str | None = None


class PipelineReportPayload(BaseModel):
    report_id: str
    period: str
    data_completeness: DataCompletenessOut = DataCompletenessOut(status="COMPLETE", gaps=[])
    funnel: list[FunnelStageOut] = []
    overall_conversion_pct: float | None = None
    carrier_performance: list[CarrierPerformanceOut] = []
    remarketing_value: list[RemarketOutcomeOut] = []
