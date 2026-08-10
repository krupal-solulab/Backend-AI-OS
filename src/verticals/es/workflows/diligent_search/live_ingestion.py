"""Additive real-ingestion path for Diligent Search & Compliance
Documentation: discovers real ``diligent_search`` review items already
seeded, for real, by Market Matching's MM-07 rule
(``verticals/es/diligent_search_hooks.py``) — never a fixture.

Honest limitation, by design, not oversight: this is the vertical's highest
legal-stakes workflow (PRD §8 — a wrongly generated affidavit is "a
potentially fraudulent record"), and no real per-state legal requirement
data or real per-carrier declination evidence exists anywhere in this
codebase. Rather than approximate that (which the PRD explicitly treats as
needing real legal sign-off, not an engineering workaround), the broker/
compliance user supplies the real per-state facts and real declination
records they possess directly — this module only discovers WHICH real
submissions are waiting for that human input, it never invents the input
itself. ``compliance_engine.determine_state``'s strict DS-01..DS-04 gate is
untouched; broker-supplied data flows through the exact same logic the
fixture path already uses.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow

WORKFLOW_NAME = "diligent_search"


async def discover_live_stub_submissions(session: AsyncSession, ctx: Ctx) -> list[dict[str, Any]]:
    """Real ``diligent_search`` review items still holding the raw MM-07-
    seeded stub (``fire_diligent_search_required``'s honestly-empty
    ``ComplianceRecordPayload`` — zero states determined yet) — these are
    the real submissions waiting for a human to complete the actual
    search. Once a broker has submitted real per-state data via
    ``run-live`` the payload no longer matches this shape, so a completed
    item naturally drops off this list rather than needing a separate
    "already done" flag."""
    rows = (
        await session.execute(
            select(ReviewItemRow, OutputPackageRow)
            .join(OutputPackageRow, col(ReviewItemRow.output_package_id) == col(OutputPackageRow.id))
            .where(
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.workflow) == WORKFLOW_NAME,
            )
        )
    ).all()
    stubs = []
    for item, pkg in rows:
        payload = pkg.payload or {}
        if not payload.get("state_determinations") and payload.get("overall_status") == "BLOCKED":
            stubs.append({"item_id": item.id, "submission_id": item.submission_id})
    return stubs
