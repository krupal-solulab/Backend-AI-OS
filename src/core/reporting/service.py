"""Group-by rollups over the audit log."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.models import AuditEntry as AuditEntryRow

# Whitelist of dimensions callers may group by (maps to a column).
_DIMENSIONS = {
    "workflow": AuditEntryRow.workflow,
    "actor": AuditEntryRow.actor,
    "who": AuditEntryRow.who,
}


class ReportingService(Protocol):
    async def rollup(
        self, session: AsyncSession, ctx: Ctx, dimension: str, period: str | None = None
    ) -> dict[str, Any]: ...


class DefaultReportingService:
    async def rollup(
        self, session: AsyncSession, ctx: Ctx, dimension: str, period: str | None = None
    ) -> dict[str, Any]:
        column = _DIMENSIONS.get(dimension)
        if column is None:
            raise ValueError(
                f"unknown dimension '{dimension}'; allowed: {sorted(_DIMENSIONS)}"
            )
        stmt = (
            select(column, func.count())
            .where(col(AuditEntryRow.tenant_id) == ctx.tenant_id)
            .group_by(column)
        )
        rows = (await session.execute(stmt)).all()
        counts = {str(value): int(n) for value, n in rows}
        return {
            "dimension": dimension,
            "period": period,
            "counts": counts,
            "total": sum(counts.values()),
        }
