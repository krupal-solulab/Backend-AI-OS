"""Resolves the subject line for a ``NO_RESPONSE_FOLLOWUP`` draft per FR-10:
reuse the original request's subject line ('Re: ...') rather than break the
thread. Looks up the actual persisted prior draft first (most correct — reuses
whatever subject was really used, even if a human edited it); falls back to a
deterministic reconstruction only if no matching prior draft exists in this
workflow's own history (e.g. the original communication predates this system,
or was never run through it)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow


async def resolve_followup_subject(
    session: AsyncSession,
    ctx: Ctx,
    *,
    workflow: str,
    submission_id: str | None,
    original_request_type: str | None,
    carrier_name: str | None,
    named_insured: str | None,
) -> str:
    if submission_id:
        stmt = (
            select(ReviewItemRow)
            .where(
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.workflow) == workflow,
                col(ReviewItemRow.submission_id) == submission_id,
            )
            .order_by(col(ReviewItemRow.created_at).desc())
        )
        items = (await session.execute(stmt)).scalars().all()
        for item in items:
            if not item.output_package_id:
                continue
            pkg = (
                await session.execute(
                    select(OutputPackageRow).where(
                        col(OutputPackageRow.id) == item.output_package_id
                    )
                )
            ).scalar_one_or_none()
            if pkg is None or not pkg.payload:
                continue
            payload = pkg.payload
            if original_request_type and payload.get("trigger_type") != original_request_type:
                continue
            if carrier_name and payload.get("carrier_name") not in (None, carrier_name):
                continue
            subject = payload.get("subject_line")
            if subject:
                return subject if subject.lower().startswith("re:") else f"Re: {subject}"

    # Fallback: no matching prior draft in this workflow's own history.
    tail = f" - {carrier_name}" if carrier_name else ""
    return f"Re: {named_insured or 'Submission'}{tail} Follow-up"
