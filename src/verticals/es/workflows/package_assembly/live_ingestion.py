"""Additive real-ingestion path for Package Assembly: builds a
``carrier_view``-shaped dict from an ACTUAL Market Matching review item,
instead of the static Workflow_11 fixture (``scenario_loader.py``, untouched
by this module) — the genuine Market Matching -> Package Assembly hand-off
the frontend already navigates for but the backend has always ignored (see
the banner this replaces in ``Workflows.tsx``).

Honest limitations, by design, not oversight:
- The real Carrier Appetite Profile panel (``decision_core.carrier_profiles``,
  already reused throughout this vertical) has no ``supplemental_form`` /
  ``supplemental_fields_auto_fillable`` / ``preferred_form_versions``
  fields — these stay absent on the live path, exactly like
  ``assemble_package`` already handles for any fixture carrier that doesn't
  define them (no supplemental fields attempted; PA-05's disclosure check
  already handles an absent ``preferred_form_versions`` as a no-op).
- Real document extraction only identifies document TYPE (ACORD/loss run/
  financials/SOV), never an exact form edition (e.g. "ACORD 125" vs "ACORD
  140") or a loss run's covered-year count. So this module's own
  completeness check (``live_completeness_check`` below) matches by
  document TYPE only — any ACORD on file satisfies any ACORD-type
  requirement, any loss run on file satisfies a loss-run requirement
  regardless of the carrier's stated minimum year count. This is a real,
  coarser simplification versus the fixture path's exact-string/year-aware
  ``check_document`` (``assembly.py``, left 100% untouched — only passed in
  as an override via ``assemble_package``'s new ``document_check_fn``
  parameter for this path specifically).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.documents.store import LocalDocumentStore
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from verticals.es.decision_core.carrier_profiles import load_carrier_panel
from verticals.es.workflows.package_assembly.diligent_search_lookup import (
    real_diligent_search_status,
)

_CARRIER_PANEL_WORKFLOW_N = 10  # same real panel reused throughout this vertical

_KIND_KEYWORDS: dict[str, str] = {
    "acord": "acord",
    "loss run": "loss_run",
    "financ": "financials",
    "statement of values": "sov",
    "sov": "sov",
}


def _requirement_kind(requirement: str) -> str | None:
    req_norm = requirement.lower()
    for keyword, kind in _KIND_KEYWORDS.items():
        if keyword in req_norm:
            return kind
    return None


def live_completeness_check(
    requirement: str, available_kinds: list[str], _missing_info: list[dict[str, Any]]
) -> tuple[bool, str | None]:
    """The live path's own, coarser completeness check — matches by
    document TYPE only. Passed into ``assemble_package`` as
    ``document_check_fn``; ``assembly.check_document`` itself stays
    untouched for the fixture-scenario path. See module docstring for
    exactly what this trades away."""
    kind = _requirement_kind(requirement)
    if kind is not None and kind in available_kinds:
        return True, None
    return False, "not on file (real document check — matches by type, not exact edition)"


async def _real_market_matching_payload(
    session: AsyncSession, ctx: Ctx, market_matching_review_item_id: str
) -> dict[str, Any]:
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == market_matching_review_item_id,
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.workflow) == "market_matching",
            )
        )
    ).scalar_one_or_none()
    if item is None or not item.output_package_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no market-matching review item '{market_matching_review_item_id}'",
        )
    pkg = (
        await session.execute(
            select(OutputPackageRow).where(col(OutputPackageRow.id) == item.output_package_id)
        )
    ).scalar_one_or_none()
    if pkg is None or not pkg.payload:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no market-matching payload for this item")
    return pkg.payload


async def discover_live_carrier_ids(
    session: AsyncSession, ctx: Ctx, market_matching_review_item_id: str
) -> list[str]:
    """Every carrier this real Market Matching item actually matched — the
    live equivalent of ``scenario_loader.all_carrier_ids`` (FR-2/FR-23: fan
    out one independent assembly pass per matched carrier)."""
    payload = await _real_market_matching_payload(session, ctx, market_matching_review_item_id)
    return [m["carrier_id"] for m in payload.get("matches", [])]


async def build_live_carrier_view(
    session: AsyncSession, ctx: Ctx, market_matching_review_item_id: str, carrier_id: str
) -> dict[str, Any]:
    """The live-data equivalent of ``market_matching_output.json``'s
    per-carrier shape, built from a real Market Matching item, the real
    Carrier Appetite Profile panel, and the submission's real persisted
    documents."""
    payload = await _real_market_matching_payload(session, ctx, market_matching_review_item_id)
    match = next(
        (m for m in payload.get("matches", []) if m.get("carrier_id") == carrier_id), None
    )
    if match is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"carrier '{carrier_id}' is not among this submission's matched carriers",
        )

    panel = load_carrier_panel(_CARRIER_PANEL_WORKFLOW_N)
    profile = next((p for p in panel if p.carrier_id == carrier_id), None)
    required_documents = (
        list(profile.submission_requirements.required_documents) if profile else []
    )

    submission_id = payload.get("submission_id")
    documents_available: list[str] = []
    if submission_id:
        docs = await LocalDocumentStore().list_for_submission(session, ctx, submission_id)
        documents_available = sorted({d.kind.value for d in docs})

    # MarketMatchingPayload's own diligent_search shape ({required, on_file,
    # compliant, note}, per market_matching/schema.py) uses `compliant`
    # where assemble_package expects `documentation_status` — normalize
    # before falling back to it (real "is documentation already sufficient"
    # signal, not a fabricated one).
    mm_diligent_search = payload.get("diligent_search") or {}
    diligent_search = {
        "documentation_status": "present" if mm_diligent_search.get("compliant") else "absent",
        "required": mm_diligent_search.get("required", False),
    }
    if submission_id:
        real_status = await real_diligent_search_status(session, ctx, submission_id)
        if real_status is not None:
            diligent_search = real_status

    return {
        "submission_id": submission_id,
        # not carried on MarketMatchingPayload (same documented v1 limitation
        # as agent_communication_hooks.py) — honestly absent, not guessed.
        "named_insured": None,
        "carrier_id": carrier_id,
        "carrier_name": match.get("carrier_name", carrier_id),
        "carrier_requirements": {"required_documents": required_documents},
        "documents_available_from_extraction": documents_available,
        "missing_info_from_market_matching": [
            {"item": m} for m in match.get("missing", [])
        ],
        "diligent_search": diligent_search,
    }
