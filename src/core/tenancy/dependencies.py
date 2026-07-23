"""FastAPI dependency that builds the request ``Ctx`` from stub headers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.common.enums import Role, Vertical
from core.db import get_session
from core.models import Tenant


async def get_ctx(
    session: Annotated[AsyncSession, Depends(get_session)],
    x_tenant_id: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
    x_role: Annotated[str | None, Header()] = None,
    x_vertical: Annotated[str | None, Header()] = None,
) -> Ctx:
    """Resolve tenant + vertical + user + role for the request.

    STUB auth (Phase 0): trusts the headers. ``vertical`` is taken from the Tenant
    row; if the tenant isn't seeded, it falls back to the ``x-vertical`` header.
    """
    if not x_tenant_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing x-tenant-id header")
    if not x_user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing x-user-id header")

    try:
        role = Role(x_role) if x_role else Role.JUNIOR
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid role: {x_role}") from None

    tenant = (
        await session.execute(select(Tenant).where(col(Tenant.id) == x_tenant_id))
    ).scalar_one_or_none()

    if tenant is not None:
        vertical = Vertical(tenant.vertical)
    elif x_vertical:
        try:
            vertical = Vertical(x_vertical)
        except ValueError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"invalid x-vertical: {x_vertical}"
            ) from None
    else:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown tenant '{x_tenant_id}' and no x-vertical fallback provided",
        )

    return Ctx(tenant_id=x_tenant_id, vertical=vertical, user_id=x_user_id, role=role)


CtxDep = Annotated[Ctx, Depends(get_ctx)]
