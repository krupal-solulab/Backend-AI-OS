"""Additive real-ingestion path for Renewal Remarketing (FR-2): builds a
``renewal_context.json``-shaped dict from an ACTUAL Binder Issuance bind
record plus real Endorsement Processing history for that same bind, instead
of the static Workflow_16 fixture (``scenario_loader.py``, untouched by this
module) — the genuine "current, accurate account profile" hand-off FR-2
always described: "combining the original bind record... with any
subsequent endorsement history... exposure changes already reflected in an
endorsement must not be re-treated as new information at renewal."

Honest limitations, by design, not oversight (all confirmed against the
real schemas, not assumed):
- Endorsement Processing's real payload has NO numeric exposure/TIV delta
  field at all (the input value exists only transiently during that
  workflow's own ingest and is never carried into its output) — so
  ``exposure_change.pct_change`` always stays ``0.0`` here, never a guessed
  percentage. The real, honest signal this path DOES carry is whether a
  material (``UNDERWRITING_REVIEW_REQUIRED``) endorsement already exists for
  this bind — RR-01's actual Scenario 03 concern — surfaced via a `note`
  that naturally satisfies `remarket_engine.py`'s existing
  `_ALREADY_EXPLAINED_PATTERNS` regex (unchanged), not a new bypass.
- Loss history (RR-02) has no source anywhere in this codebase — no claims
  workflow exists. `expiring_term_loss_activity` stays `""`.
- Incumbent renewal-offer status (RR-03/RR-07) has no source either — an
  incoming renewal offer is an external carrier document neither Binder
  Issuance nor Endorsement Processing produce. `incumbent_renewal_offer`
  stays `{}`.
- Both defaults above resolve through the existing, unmodified
  `detect_loss_history_change`/`check_incumbent_status` functions to their
  safe "nothing unusual" values — never fabricated, never guessed.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.models import OutputPackage as OutputPackageRow


async def _binder_issuance_rows(session: AsyncSession, ctx: Ctx) -> list[OutputPackageRow]:
    rows = (
        await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.workflow) == "binder_issuance",
            )
        )
    ).scalars().all()
    return list(rows)


async def discover_live_binds(session: AsyncSession, ctx: Ctx) -> list[dict[str, Any]]:
    """Every real Binder Issuance bind for this tenant — so a broker never
    has to already know a bind_id to check it."""
    rows = await _binder_issuance_rows(session, ctx)
    binds = []
    for row in rows:
        if not row.payload or not row.payload.get("bind_id"):
            continue
        binds.append({
            "bind_id": row.payload["bind_id"],
            "named_insured": row.payload.get("named_insured"),
            "carrier_name": row.payload.get("carrier_name"),
        })
    return binds


async def build_live_renewal_context(
    session: AsyncSession, ctx: Ctx, bind_id: str
) -> dict[str, Any]:
    """The live-data equivalent of ``scenario_loader.load_scenario()``'s
    trigger-stage ``renewal_context.json`` shape, built from a real bind
    plus real endorsement history for it."""
    binder_rows = await _binder_issuance_rows(session, ctx)
    bind_payload = next(
        (r.payload for r in binder_rows if r.payload and r.payload.get("bind_id") == bind_id),
        None,
    )
    if bind_payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no binder-issuance bind '{bind_id}'")

    endorsement_rows = (
        await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.workflow) == "endorsement",
                col(OutputPackageRow.submission_id) == bind_id,
            )
        )
    ).scalars().all()
    material_endorsements = [
        r.payload for r in endorsement_rows
        if r.payload and r.payload.get("classification") == "UNDERWRITING_REVIEW_REQUIRED"
    ]

    if material_endorsements:
        detail = material_endorsements[0].get("requested_change", {}).get("detail", "")
        note = (
            f"This exposure change was already endorsed mid-term "
            f"({detail}) — see Endorsement Processing record for this bind."
        )
    else:
        note = None

    return {
        "bind_id": bind_id,
        "named_insured": bind_payload.get("named_insured"),
        "incumbent_carrier_id": bind_payload.get("carrier_id"),
        "incumbent_carrier_name": bind_payload.get("carrier_name", ""),
        "exposure_change": {"pct_change": 0.0, "note": note},
        "expiring_term_loss_activity": "",
        "incumbent_renewal_offer": {},
        "remarketing_history": None,
    }
