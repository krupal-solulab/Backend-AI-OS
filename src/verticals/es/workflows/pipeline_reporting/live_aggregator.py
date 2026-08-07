"""Additive live-aggregation path for Pipeline & Carrier Performance
Reporting (PR-01/PR-02/PR-05).

Builds a real funnel / carrier-performance / remarketing-value report from
actual ``OutputPackage`` rows across the five source workflows for this
tenant, instead of the static Workflow_19 fixture (``scenario_loader.py``,
untouched by this module) — the genuine cross-workflow aggregation this
PRD's FR-1 always described ("aggregate stage-level counts directly from
each source workflow's own logged events").

Honest limitations, by design, not oversight:
- PR-04 (revenue attribution) has no source data anywhere in this codebase
  — not attempted here either, exactly like ``reporting_engine.py``.
- A remarketing "savings" figure is only ever computed when a remarket was
  genuinely initiated, resulted in a real carrier switch
  (``final_decision.outcome == "switched_carrier"``), the comparison was
  directly comparable, and both incumbent/alternative premiums are
  present. No workflow in this codebase currently has an action that
  records a real carrier-switch decision, so today every live remarket
  outcome honestly resolves to ``confirmation_value``/``not_remarketed`` —
  never a guessed dollar figure (PR-05's own zero-tolerance rule).
- Carrier performance groups by carrier NAME — the one join key present
  consistently across Package Assembly / Quote Comparison / Binder
  Issuance's real payloads (none of them reliably carry the same
  ``carrier_id`` for a given carrier).
- Time-to-placement (PR-03) reports RAW elapsed time (earliest real Market
  Matching row for a submission_id -> the real Binder Issuance row where a
  ``carrier_confirmation.binder_number`` is actually set) — never a
  fabricated one. FR-4's broker/agent-delay exclusion is NOT computed:
  Package Assembly only stores a submission's CURRENT status, never a
  history of when it entered/left BLOCKED, so there is no real data
  anywhere to measure that exclusion duration from (see
  ``TimeToPlacementOut`` in schema.py).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.models import OutputPackage as OutputPackageRow


async def _payloads_for(session: AsyncSession, ctx: Ctx, workflow: str) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.workflow) == workflow,
            )
        )
    ).scalars().all()
    return [r.payload for r in rows if r.payload]


async def _rows_for(session: AsyncSession, ctx: Ctx, workflow: str) -> list[OutputPackageRow]:
    rows = (
        await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.workflow) == workflow,
            )
        )
    ).scalars().all()
    return [r for r in rows if r.payload]


def _build_funnel_data(
    mm_rows: list[dict[str, Any]],
    pa_rows: list[dict[str, Any]],
    qc_rows: list[dict[str, Any]],
    bi_rows: list[dict[str, Any]],
) -> dict[str, int]:
    submissions_received = len(
        {r["submission_id"] for r in mm_rows if r.get("submission_id")}
    )
    matched_to_carrier = sum(1 for r in mm_rows if r.get("matches"))
    packages_assembled = len({
        r["submission_id"]
        for r in pa_rows
        if r.get("submission_id") and r.get("status") in ("READY", "READY_WITH_GAP")
    })
    quotes_received = len({
        r["submission_id"]
        for r in qc_rows
        if r.get("submission_id")
        and any(q.get("response_type") == "QUOTE" for q in r.get("quotes", []))
    })
    compared_and_selected = sum(1 for r in qc_rows if r.get("selected_quote_id"))
    bound = sum(1 for r in bi_rows if (r.get("carrier_confirmation") or {}).get("binder_number"))
    return {
        "submissions_received": submissions_received,
        "matched_to_carrier": matched_to_carrier,
        "packages_assembled": packages_assembled,
        "quotes_received": quotes_received,
        "compared_and_selected": compared_and_selected,
        "bound": bound,
    }


def _build_carrier_activity(
    pa_rows: list[dict[str, Any]], qc_rows: list[dict[str, Any]], bi_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    approached: dict[str, int] = defaultdict(int)
    quoted: dict[str, int] = defaultdict(int)
    binds: dict[str, int] = defaultdict(int)

    for r in pa_rows:
        name = r.get("carrier_name")
        if name:
            approached[name] += 1
    for r in qc_rows:
        for q in r.get("quotes", []):
            name = q.get("carrier_name")
            if name and q.get("response_type") == "QUOTE":
                quoted[name] += 1
    for r in bi_rows:
        name = r.get("carrier_name")
        if name and (r.get("carrier_confirmation") or {}).get("binder_number"):
            binds[name] += 1

    return [
        {
            "carrier_name": name,
            "submissions_approached": approached[name],
            "quotes_issued": quoted.get(name, 0),
            "binds": binds.get(name, 0),
        }
        for name in sorted(approached)  # approached > 0 for every key here, by construction
    ]


def _build_time_to_placement_data(
    mm_rows: list[OutputPackageRow], bi_rows: list[OutputPackageRow]
) -> list[dict[str, Any]]:
    """PR-03: raw elapsed time from a submission's earliest real Market
    Matching row to the real Binder Issuance row where it was actually
    bound (a real ``carrier_confirmation.binder_number`` on file) — joined
    by ``submission_id``, the same real join key ``_build_funnel_data``
    already relies on across these same workflows. FR-4's delay exclusion
    is NOT applied — see this module's docstring."""
    matched_at: dict[str, Any] = {}
    for r in mm_rows:
        sub_id = (r.payload or {}).get("submission_id")
        if not sub_id:
            continue
        if sub_id not in matched_at or r.created_at < matched_at[sub_id]:
            matched_at[sub_id] = r.created_at

    placements: list[dict[str, Any]] = []
    for r in bi_rows:
        payload = r.payload or {}
        sub_id = payload.get("submission_id")
        carrier_name = payload.get("carrier_name")
        binder_number = (payload.get("carrier_confirmation") or {}).get("binder_number")
        if not sub_id or not carrier_name or not binder_number:
            continue
        start = matched_at.get(sub_id)
        if start is None:
            continue
        days = (r.created_at.date() - start.date()).days
        if days < 0:
            continue  # out-of-order/clock-skew data — never report a negative elapsed time
        placements.append({"carrier_name": carrier_name, "days": days})
    return placements


def _build_remarket_outcomes(rr_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_account: dict[str, dict[str, Any]] = {}
    for r in rr_rows:
        name = r.get("named_insured") or "Unknown account"
        entry = by_account.setdefault(name, {})
        if r.get("is_comparison_stage"):
            entry["execution"] = r.get("remarket_execution") or {}
            entry["final_decision"] = r.get("final_decision") or {}
        else:
            trigger_decision = r.get("trigger_decision") or {}
            entry["trigger_level"] = trigger_decision.get("level", "NO_REMARKET")
            entry["reasoning_summary"] = (trigger_decision.get("reasoning") or {}).get("summary")

    outcomes = []
    for name, entry in by_account.items():
        trigger = entry.get("trigger_level", "NO_REMARKET")
        execution = entry.get("execution") or {}
        comparison = execution.get("comparison_output") or {}
        final = entry.get("final_decision") or {}

        savings: float | None = None
        if (
            trigger != "NO_REMARKET"
            and execution.get("initiated")
            and final.get("outcome") == "switched_carrier"
            and comparison.get("directly_comparable")
        ):
            incumbent_premium = (comparison.get("incumbent") or {}).get("premium")
            alternative_premium = (comparison.get("alternative") or {}).get("premium")
            if incumbent_premium is not None and alternative_premium is not None:
                savings = incumbent_premium - alternative_premium

        outcomes.append({
            "account": name,
            "trigger": trigger.lower(),
            "savings_identified": savings,
            "note": entry.get("reasoning_summary"),
        })
    return outcomes


async def build_live_underlying_data(session: AsyncSession, ctx: Ctx) -> dict[str, Any]:
    """The live-data equivalent of ``scenario_loader.load_scenario()``'s
    ``underlying_data.json`` shape, built from real cross-workflow rows.
    Unlike a single fixture scenario (which always exercises exactly one
    report "kind"), this returns funnel + carrier + remarketing data all at
    once — genuinely everything logged so far for this tenant."""
    mm_rows_raw = await _rows_for(session, ctx, "market_matching")
    bi_rows_raw = await _rows_for(session, ctx, "binder_issuance")
    mm_rows = [r.payload for r in mm_rows_raw]
    pa_rows = await _payloads_for(session, ctx, "package_assembly")
    qc_rows = await _payloads_for(session, ctx, "quote_comparison")
    bi_rows = [r.payload for r in bi_rows_raw]
    rr_rows = await _payloads_for(session, ctx, "renewal_remarketing")

    return {
        **_build_funnel_data(mm_rows, pa_rows, qc_rows, bi_rows),
        "carrier_activity": _build_carrier_activity(pa_rows, qc_rows, bi_rows),
        "placements": _build_time_to_placement_data(mm_rows_raw, bi_rows_raw),
        "remarket_outcomes": _build_remarket_outcomes(rr_rows),
    }
