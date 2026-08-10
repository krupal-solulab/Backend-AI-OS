"""Routes under ``/api/core/auth`` — email-based-role login (still Phase-0
stub auth, no password/credential check).

The frontend's header-stub auth (``core/tenancy/dependencies.get_ctx``) is
unchanged — it still just trusts whatever ``x-tenant-id``/``x-user-id``/
``x-role`` headers arrive. This endpoint's only job is to tell the frontend
WHICH headers to start sending for a given email, by looking that email up
in the real ``User`` table (``core/seed.py`` seeds it) — never guessing a
role for an email it doesn't recognize.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.enums import Vertical
from core.db import get_session
from core.models import Tenant, User

router = APIRouter(prefix="/auth", tags=["core:auth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class LoginRequest(BaseModel):
    email: str


class LoginOut(BaseModel):
    tenant_id: str
    user_id: str
    email: str
    name: str | None
    role: str
    vertical: str


@router.post("/login")
async def login(body: LoginRequest, session: SessionDep) -> LoginOut:
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "email is required")

    user = (
        await session.execute(select(User).where(col(User.email) == email))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user found for '{body.email}'")

    tenant = (
        await session.execute(select(Tenant).where(col(Tenant.id) == user.tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"user's tenant '{user.tenant_id}' no longer exists"
        )

    return LoginOut(
        tenant_id=user.tenant_id,
        user_id=user.id,
        email=user.email,
        name=user.name,
        role=user.role.value,
        vertical=Vertical(tenant.vertical).value,
    )
