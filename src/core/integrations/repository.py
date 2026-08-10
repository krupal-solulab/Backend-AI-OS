"""CRUD over the ``connection`` table (tenant <-> Nango provider connection)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.models import Connection


async def get_connection(
    session: AsyncSession, tenant_id: str, provider: str
) -> Connection | None:
    return (
        await session.execute(
            select(Connection).where(
                col(Connection.tenant_id) == tenant_id, col(Connection.provider) == provider
            )
        )
    ).scalar_one_or_none()


async def list_connections(session: AsyncSession, tenant_id: str) -> list[Connection]:
    return (
        (await session.execute(select(Connection).where(col(Connection.tenant_id) == tenant_id)))
        .scalars()
        .all()
    )


async def upsert_connection(
    session: AsyncSession,
    tenant_id: str,
    provider: str,
    *,
    nango_connection_id: str | None = None,
    status: str = "connected",
) -> Connection:
    row = await get_connection(session, tenant_id, provider)
    if row is None:
        row = Connection(tenant_id=tenant_id, provider=provider)
        session.add(row)
    row.status = status
    if nango_connection_id is not None:
        row.nango_connection_id = nango_connection_id
    await session.commit()
    await session.refresh(row)
    return row
