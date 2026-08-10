"""Routes under ``/api/core/dashboard`` — one real, cross-workflow overview
for the app's home page. See ``service.py``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import Ctx
from core.dashboard.service import build_dashboard_overview
from core.db import get_session
from core.tenancy.dependencies import get_ctx

router = APIRouter(prefix="/dashboard", tags=["core:dashboard"])

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class FunnelStageOut(BaseModel):
    stage: str
    count: int | None
    pct_of_prior_stage: float | None


class DataGapOut(BaseModel):
    stage: str
    reason: str


class FunnelOut(BaseModel):
    stages: list[FunnelStageOut]
    gaps: list[DataGapOut]
    overall_conversion_pct: float | None


class CarrierPerformanceOut(BaseModel):
    carrier_name: str
    submissions_approached: int
    quote_rate: float
    bind_rate: float
    overall_hit_rate: float
    low_volume_flag: bool


class TimeToPlacementOut(BaseModel):
    carrier_name: str
    submissions_bound: int
    avg_days: float
    low_volume_flag: bool


class RemarketOutcomeOut(BaseModel):
    account: str
    trigger_level: str
    outcome_type: str
    savings_amount: float | None
    note: str | None


class RecentActivityOut(BaseModel):
    workflow: str
    ref: str
    status: str
    created_at: str


class RemarketPipelineItemOut(BaseModel):
    named_insured: str | None
    incumbent_carrier_name: str | None
    trigger_level: str | None
    created_at: str


class DiligentSearchAlertOut(BaseModel):
    submission_id: str | None
    on_file: int | None
    note: str | None


class DailyPipelinePointOut(BaseModel):
    date: str
    submissions: int
    bound: int


class DashboardOverviewOut(BaseModel):
    generated_at: str
    workflow_counts: dict[str, int]
    submissions_today: int
    quotes_received_today: int
    remarket_pending: int
    endorsement_pending: int
    binders_confirmed: int
    bound_premium_mtd: float
    funnel: FunnelOut
    carrier_performance: list[CarrierPerformanceOut]
    time_to_placement: list[TimeToPlacementOut]
    remarket_value: list[RemarketOutcomeOut]
    recent_activity: list[RecentActivityOut]
    remarket_pipeline: list[RemarketPipelineItemOut]
    diligent_search_alerts: list[DiligentSearchAlertOut]
    daily_pipeline: list[DailyPipelinePointOut]


@router.get("/overview")
async def get_dashboard_overview(ctx: CtxDep, session: SessionDep) -> DashboardOverviewOut:
    data = await build_dashboard_overview(session, ctx)
    return DashboardOverviewOut(**data)
