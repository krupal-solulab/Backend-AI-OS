"""Additive live-aggregation path for Carrier Appetite Intelligence (CI-01).

Builds a real signal log from Quote Comparison's actual ``OutputPackage``
rows for this tenant, instead of the static Workflow_18 fixture
(``scenario_loader.py``, untouched by this module) — the genuine
cross-workflow read CI-01 always described: QC-03's declination-consistency
signal, already logged by Quote Comparison, read back here rather than
re-derived or invented.

Renewal Remarketing's RR-08 was checked against this same design (per the
approved connectivity plan) and genuinely does not carry a decline/appetite
signal in its real payload (no reason/outcome/class_code fields exist there)
— it is intentionally excluded here rather than forced into a shape it
doesn't have.

Honest limitations, by design, not oversight:
- Quote Comparison's real payload never sets ``carrier_id`` on a quote (only
  ``carrier_name``), so this joins on carrier NAME, not id.
- Quote Comparison never persists the declination's actual response date, so
  ``date`` here is the ``OutputPackage`` row's ``created_at`` (ingestion
  time) — an honest approximation, not the real signal date.
- ``class_code`` isn't present anywhere in Quote Comparison's payload, so
  live evaluations aggregate per-carrier across all classes, not per class
  like the fixture-scenario path does — a real reduction in granularity,
  not a fabrication.
- A quote whose ``declination_appetite_consistency`` is
  ``"unable_to_determine"`` is excluded entirely, never defaulted to
  consistent or inconsistent.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.models import OutputPackage as OutputPackageRow
from verticals.es.decision_core.carrier_profiles import load_carrier_panel

_CARRIER_PANEL_WORKFLOW_N = 10  # same real panel service.py's fixture path already reuses


async def _quote_comparison_rows(session: AsyncSession, ctx: Ctx) -> list[OutputPackageRow]:
    rows = (
        await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.workflow) == "quote_comparison",
            )
        )
    ).scalars().all()
    return list(rows)


def _classifiable_declinations(rows: list[OutputPackageRow]):
    for row in rows:
        if not row.payload:
            continue
        for q in row.payload.get("quotes", []):
            if q.get("response_type") != "DECLINATION":
                continue
            consistency = q.get("declination_appetite_consistency")
            if consistency not in ("consistent", "inconsistent"):
                continue  # "unable_to_determine" — excluded, never guessed
            yield row, q, consistency


async def discover_live_carriers(session: AsyncSession, ctx: Ctx) -> list[dict[str, str]]:
    """Every carrier with at least one real, classifiable declination signal
    logged by Quote Comparison for this tenant — so a broker never has to
    already know a carrier name to check it."""
    rows = await _quote_comparison_rows(session, ctx)
    names = {q["carrier_name"] for _row, q, _c in _classifiable_declinations(rows) if q.get("carrier_name")}

    panel = load_carrier_panel(_CARRIER_PANEL_WORKFLOW_N)
    by_name = {p.carrier_name: p.carrier_id for p in panel}
    return [
        {"carrier_id": by_name.get(name, name), "carrier_name": name} for name in sorted(names)
    ]


async def build_live_signal_log(
    session: AsyncSession, ctx: Ctx, carrier_name: str
) -> dict[str, Any]:
    """The live-data equivalent of ``scenario_loader.load_scenario()``'s
    return shape — same ``{carrier_id, carrier_name, class_code,
    observed_outcomes}`` contract the pipeline already consumes, just built
    from real rows instead of a fixture file."""
    rows = await _quote_comparison_rows(session, ctx)
    observed_outcomes: list[dict[str, Any]] = [
        {
            "submission_id": q.get("submission_id") or row.submission_id,
            "date": row.created_at.date().isoformat(),
            "outcome": "declined",
            "reason_given": q.get("declination_reason"),
            "consistent_with_profile": consistency == "consistent",
        }
        for row, q, consistency in _classifiable_declinations(rows)
        if q.get("carrier_name") == carrier_name
    ]

    panel = load_carrier_panel(_CARRIER_PANEL_WORKFLOW_N)
    profile = next((p for p in panel if p.carrier_name == carrier_name), None)
    return {
        "carrier_id": profile.carrier_id if profile else carrier_name,
        "carrier_name": carrier_name,
        "class_code": "ALL",  # no class_code exists in Quote Comparison's real payload
        "observed_outcomes": observed_outcomes,
    }
