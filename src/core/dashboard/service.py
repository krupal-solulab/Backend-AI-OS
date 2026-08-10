"""Real, cross-workflow dashboard data — every number here is computed from
real ``ReviewItem``/``OutputPackage`` rows, never a fixed/example value.

Reuses Pipeline Reporting's own pure aggregation functions (funnel, carrier
performance, time-to-placement, remarket outcomes) rather than re-deriving
duplicate logic — those functions make no LLM call, so this module never
depends on the LLM being available either. The handful of KPIs Pipeline
Reporting doesn't cover (today's counts, bound premium, remarket pipeline,
diligent-search alerts) get their own light, direct queries below.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.assistant.service import WORKFLOW_LABELS
from core.common.dtos import Ctx
from core.common.enums import ReviewStatus
from core.models import OutputPackage, ReviewItem
from verticals.es.workflows.pipeline_reporting.live_aggregator import build_live_underlying_data
from verticals.es.workflows.pipeline_reporting.reporting_engine import (
    build_carrier_performance,
    build_funnel,
    build_time_to_placement,
    categorize_remarket_outcome,
)

_RECENT_ACTIVITY_LIMIT = 12
_REMARKET_PIPELINE_LIMIT = 6
_DILIGENT_SEARCH_ALERT_LIMIT = 5
_DAILY_PIPELINE_DAYS = 7


async def _all_rows(session: AsyncSession, tenant_id: str) -> list[tuple[ReviewItem, OutputPackage]]:
    result = await session.execute(
        select(ReviewItem, OutputPackage)
        .join(OutputPackage, col(ReviewItem.output_package_id) == col(OutputPackage.id))
        .where(col(ReviewItem.tenant_id) == tenant_id)
        .order_by(col(ReviewItem.created_at).desc())
    )
    return list(result.all())


def _label_for(workflow: str) -> str:
    return WORKFLOW_LABELS.get(workflow, workflow)


def _recent_activity_detail(payload: dict[str, Any], ri: ReviewItem) -> str:
    return (
        payload.get("named_insured")
        or payload.get("submission_id")
        or ri.submission_id
        or ri.id
    )


async def build_dashboard_overview(session: AsyncSession, ctx: Ctx) -> dict[str, Any]:
    rows = await _all_rows(session, ctx.tenant_id)
    today = datetime.now(UTC).date()

    pending_counts: dict[str, int] = {}
    for ri, op in rows:
        if ri.status == ReviewStatus.PENDING:
            label = _label_for(op.workflow)
            pending_counts[label] = pending_counts.get(label, 0) + 1

    submissions_today = sum(
        1
        for ri, op in rows
        if op.workflow == "market_matching" and ri.created_at.date() == today
    )
    quotes_received_today = sum(
        1
        for ri, op in rows
        if op.workflow == "quote_comparison" and ri.created_at.date() == today
    )
    remarket_pending = pending_counts.get(_label_for("renewal_remarketing"), 0)
    endorsement_pending = pending_counts.get(_label_for("endorsement"), 0)

    binders_confirmed = 0
    bound_premium_mtd = 0.0
    for ri, op in rows:
        if op.workflow != "binder_issuance":
            continue
        payload = op.payload or {}
        if payload.get("bind_order_status") != "SENT":
            continue
        binders_confirmed += 1
        premium = (payload.get("requested_bind_terms") or {}).get("premium")
        if premium and ri.created_at.date().replace(day=1) == today.replace(day=1):
            bound_premium_mtd += float(premium)

    recent_activity = [
        {
            "workflow": _label_for(op.workflow),
            "ref": _recent_activity_detail(op.payload or {}, ri),
            "status": ri.status.value,
            "created_at": ri.created_at.isoformat(),
        }
        for ri, op in rows[:_RECENT_ACTIVITY_LIMIT]
    ]

    remarket_pipeline = [
        {
            "named_insured": (op.payload or {}).get("named_insured"),
            "incumbent_carrier_name": (op.payload or {}).get("incumbent_carrier_name"),
            "trigger_level": ((op.payload or {}).get("trigger_decision") or {}).get("level"),
            "created_at": ri.created_at.isoformat(),
        }
        for ri, op in rows
        if op.workflow == "renewal_remarketing"
    ][:_REMARKET_PIPELINE_LIMIT]

    diligent_search_alerts = []
    for ri, op in rows:
        if op.workflow != "market_matching":
            continue
        ds = (op.payload or {}).get("diligent_search") or {}
        if ds.get("required") and not ds.get("compliant"):
            diligent_search_alerts.append(
                {
                    "submission_id": (op.payload or {}).get("submission_id"),
                    "on_file": ds.get("on_file"),
                    "note": ds.get("note"),
                }
            )
    diligent_search_alerts = diligent_search_alerts[:_DILIGENT_SEARCH_ALERT_LIMIT]

    # Real counts over the last 7 real days — not a fabricated "trailing 7
    # months" trend; this dataset doesn't have 7 months of real history.
    daily_pipeline = []
    for i in range(_DAILY_PIPELINE_DAYS - 1, -1, -1):
        day = today - timedelta(days=i)
        subs = sum(
            1
            for ri, op in rows
            if op.workflow == "market_matching" and ri.created_at.date() == day
        )
        bound = sum(
            1
            for ri, op in rows
            if op.workflow == "binder_issuance"
            and (op.payload or {}).get("bind_order_status") == "SENT"
            and ri.created_at.date() == day
        )
        daily_pipeline.append({"date": day.isoformat(), "submissions": subs, "bound": bound})

    underlying = await build_live_underlying_data(session, ctx)
    funnel = build_funnel(underlying)
    carriers = build_carrier_performance(underlying["carrier_activity"])
    placements = build_time_to_placement(underlying["placements"])
    remarket_value = [categorize_remarket_outcome(o) for o in underlying["remarket_outcomes"]]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "workflow_counts": pending_counts,
        "submissions_today": submissions_today,
        "quotes_received_today": quotes_received_today,
        "remarket_pending": remarket_pending,
        "endorsement_pending": endorsement_pending,
        "binders_confirmed": binders_confirmed,
        "bound_premium_mtd": bound_premium_mtd,
        "funnel": dataclasses.asdict(funnel),
        "carrier_performance": [dataclasses.asdict(c) for c in carriers],
        "time_to_placement": [dataclasses.asdict(p) for p in placements],
        "remarket_value": [dataclasses.asdict(r) for r in remarket_value],
        "recent_activity": recent_activity,
        "remarket_pipeline": remarket_pipeline,
        "diligent_search_alerts": diligent_search_alerts,
        "daily_pipeline": daily_pipeline,
    }
