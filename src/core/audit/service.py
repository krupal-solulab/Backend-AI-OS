"""Append-only audit log backed by the ``audit_entry`` table."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import AuditEntry, Ctx
from core.models import AuditEntry as AuditEntryRow


class AuditService(Protocol):
    async def record(self, session: AsyncSession, ctx: Ctx, entry: AuditEntry) -> str: ...
    async def query(
        self, session: AsyncSession, ctx: Ctx, filter: dict[str, Any]
    ) -> list[AuditEntry]: ...


class DefaultAuditService:
    async def record(self, session: AsyncSession, ctx: Ctx, entry: AuditEntry) -> str:
        row = AuditEntryRow(
            tenant_id=ctx.tenant_id,
            vertical=ctx.vertical,
            actor=entry.actor,
            who=entry.who,
            what=entry.what,
            workflow=entry.workflow,
            detail=entry.detail or None,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id

    async def query(
        self, session: AsyncSession, ctx: Ctx, filter: dict[str, Any]
    ) -> list[AuditEntry]:
        stmt = select(AuditEntryRow).where(col(AuditEntryRow.tenant_id) == ctx.tenant_id)
        if "workflow" in filter:
            stmt = stmt.where(col(AuditEntryRow.workflow) == filter["workflow"])
        if "actor" in filter:
            stmt = stmt.where(col(AuditEntryRow.actor) == filter["actor"])
        stmt = stmt.order_by(col(AuditEntryRow.at))
        rows = (await session.execute(stmt)).scalars().all()
        return [
            AuditEntry(
                actor=r.actor,
                who=r.who,
                what=r.what,
                workflow=r.workflow,
                tenant_id=r.tenant_id,
                vertical=r.vertical,
                at=r.at,
                detail=r.detail or {},
            )
            for r in rows
        ]
