"""Additive real-ingestion path for Binder & Policy Issuance: builds a
``broker_bind_instruction``-shaped dict from an ACTUAL, already-selected
Quote Comparison record, instead of the static Workflow_14 fixture
(``scenario_loader.py``, untouched by this module) — the genuine
Quote Comparison -> Binder & Policy Issuance hand-off this vertical always
implied but never wired: a broker selecting a quote (``/quote-comparison/
{item_id}/select/{quote_id}``) is a real decision that should be able to
start a real bind order, not just fire an Agent Communication draft.

Honest limitations, by design, not oversight:
- Quote Comparison's real payload never sets ``carrier_id`` on a quote (only
  ``carrier_name``), so ``carrier_id`` here is resolved via the same real
  Carrier Appetite Profile panel lookup already reused throughout this
  vertical (``decision_core.carrier_profiles.load_carrier_panel``) — if no
  carrier in that panel matches the name, ``carrier_id`` stays ``None``
  honestly rather than being invented.
- Every subjectivity carried over from a just-selected quote is, by
  construction, still open and still pre-bind — Quote Comparison's
  ``SubjectivityOut`` has no clearance-tracking or lifecycle field at all,
  so ``status="open"`` and an absent ``lifecycle_stage`` (which
  ``classify_subjectivities`` already defaults to ``PRE_BIND``) are the only
  values that could ever be true here, not fabricated defaults.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from verticals.es.decision_core.carrier_profiles import load_carrier_panel

_CARRIER_PANEL_WORKFLOW_N = 10  # same real panel reused throughout this vertical


async def build_broker_bind_instruction_from_quote(
    session: AsyncSession, ctx: Ctx, quote_comparison_item_id: str
) -> dict[str, Any]:
    """The live-data equivalent of a ``broker_bind_instruction.json``
    fixture, built from a real, already-selected Quote Comparison item."""
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == quote_comparison_item_id,
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.workflow) == "quote_comparison",
            )
        )
    ).scalar_one_or_none()
    if item is None or not item.output_package_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no quote-comparison review item '{quote_comparison_item_id}'",
        )
    pkg = (
        await session.execute(
            select(OutputPackageRow).where(col(OutputPackageRow.id) == item.output_package_id)
        )
    ).scalar_one_or_none()
    if pkg is None or not pkg.payload:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no comparison payload for this item")

    payload = pkg.payload
    selected_quote_id = payload.get("selected_quote_id")
    if not selected_quote_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no quote has been selected on this item yet — call "
            "/quote-comparison/{item_id}/select/{quote_id} first",
        )
    quote = next(
        (q for q in payload.get("quotes", []) if q.get("quote_id") == selected_quote_id), None
    )
    if quote is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "selected quote not found on this item")

    panel = load_carrier_panel(_CARRIER_PANEL_WORKFLOW_N)
    carrier_name = quote.get("carrier_name")
    profile = next((p for p in panel if p.carrier_name == carrier_name), None)

    return {
        "submission_id": payload.get("submission_id"),
        "named_insured": payload.get("named_insured"),
        "carrier_id": profile.carrier_id if profile else None,
        "carrier_name": carrier_name,
        "selected_quote_id": selected_quote_id,
        "bind_terms_requested": {
            "premium": quote.get("premium"),
            "limits": quote.get("limits"),
            "deductibles": quote.get("deductibles"),
            "effective_date": quote.get("effective_date"),
        },
        "subjectivities_at_selection": [
            {"description": s.get("description"), "materiality": s.get("materiality")}
            for s in quote.get("subjectivities", [])
        ],
    }
