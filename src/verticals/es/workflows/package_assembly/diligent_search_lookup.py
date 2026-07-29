"""Additive real-data lookup for Package Assembly's PA-06: reflects an
ACTUAL Diligent Search record's status for a submission, instead of the
static Workflow_11 fixture's own ``diligent_search`` field.

Honest simplification, documented not hidden: Diligent Search's real output
is per-state (``state_determinations: [...]``); Package Assembly's existing
shape (``carrier_view["diligent_search"]``, per ``assembly.py``) is a single
flag (``documentation_status``/``required``). This reduces across states —
``documentation_status="present"`` if ANY state is ``SUFFICIENT``;
``required=True`` if ANY state is ``REQUIRED`` and not yet ``SUFFICIENT`` —
a real reduction in granularity versus a full per-state view, not a
fabrication.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.models import OutputPackage as OutputPackageRow


async def real_diligent_search_status(
    session: AsyncSession, ctx: Ctx, submission_id: str
) -> dict[str, Any] | None:
    """``None`` if no real Diligent Search record exists yet for this
    submission, OR if one exists but no state has been determined yet (a
    freshly-seeded stub, per ``diligent_search_hooks.py`` — an empty list
    means "not yet checked," never "not required") — callers should fall
    back to whatever the upstream signal says in either case."""
    row = (
        await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.workflow) == "diligent_search",
                col(OutputPackageRow.submission_id) == submission_id,
            )
        )
    ).scalars().first()
    if row is None or not row.payload:
        return None

    states = row.payload.get("state_determinations") or []
    if not states:
        return None
    present = any(s.get("sufficiency_status") == "SUFFICIENT" for s in states)
    required = any(
        s.get("requirement_status") == "REQUIRED" and s.get("sufficiency_status") != "SUFFICIENT"
        for s in states
    )
    return {"documentation_status": "present" if present else "absent", "required": required}
