"""Routes under ``/api/core/integrations`` — the tenant-facing "Connect Gmail" flow.

The frontend calls ``POST /connect-session`` to get a Nango Connect UI session
token, opens Nango's hosted popup with it, then calls ``POST /connections`` with
the ``connectionId`` Nango hands back on success. See docs/CONNECTORS_NANGO.md.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings, get_settings
from core.db import get_session
from core.integrations.repository import list_connections, upsert_connection
from core.tenancy.dependencies import get_ctx
from core.common.dtos import Ctx

router = APIRouter(prefix="/integrations", tags=["core:integrations"])

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ConnectSessionRequest(BaseModel):
    provider: str


class ConnectSessionOut(BaseModel):
    session_token: str
    expires_at: str | None = None


class ConfirmConnectionRequest(BaseModel):
    provider: str
    nango_connection_id: str


class ConnectionOut(BaseModel):
    provider: str
    status: str


@router.post("/connect-session", status_code=status.HTTP_201_CREATED)
async def create_connect_session(
    body: ConnectSessionRequest,
    ctx: CtxDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConnectSessionOut:
    """Mints a Nango Connect UI session token scoped to this tenant + the
    requested integration, so the frontend can open the hosted OAuth popup.
    ``provider`` must be one of this deployment's known Nango integration
    keys (Gmail/Sheets/Drive) — never passed through to Nango unchecked."""
    known_providers = {
        settings.nango_integration_mail,
        settings.nango_integration_sheet,
        settings.nango_integration_drive,
        settings.nango_integration_calendar,
    }
    if body.provider not in known_providers:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown provider: {body.provider}")
    if not settings.nango_secret_key:
        raise HTTPException(
            status.HTTP_412_PRECONDITION_FAILED, "NANGO_SECRET_KEY is not configured"
        )
    async with httpx.AsyncClient(base_url=settings.nango_host, timeout=30.0) as client:
        resp = await client.post(
            "/connect/sessions",
            headers={"Authorization": f"Bearer {settings.nango_secret_key}"},
            json={
                "end_user": {"id": ctx.tenant_id},
                "allowed_integrations": [body.provider],
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Nango connect-session request failed: {resp.text}"
        )
    data = resp.json().get("data", {})
    return ConnectSessionOut(session_token=data.get("token", ""), expires_at=data.get("expires_at"))


@router.post("/connections", status_code=status.HTTP_201_CREATED)
async def confirm_connection(
    body: ConfirmConnectionRequest, ctx: CtxDep, session: SessionDep
) -> ConnectionOut:
    """Called by the frontend right after the Nango Connect UI popup reports
    success — persists the tenant <-> provider <-> nango_connection_id mapping."""
    row = await upsert_connection(
        session,
        ctx.tenant_id,
        body.provider,
        nango_connection_id=body.nango_connection_id,
        status="connected",
    )
    return ConnectionOut(provider=row.provider, status=row.status)


@router.get("/connections")
async def get_connections(ctx: CtxDep, session: SessionDep) -> list[ConnectionOut]:
    rows = await list_connections(session, ctx.tenant_id)
    return [ConnectionOut(provider=r.provider, status=r.status) for r in rows]


@router.post("/connections/{provider}/disconnect")
async def disconnect(provider: str, ctx: CtxDep, session: SessionDep) -> ConnectionOut:
    row = await upsert_connection(session, ctx.tenant_id, provider, status="disconnected")
    return ConnectionOut(provider=row.provider, status=row.status)
