"""Tenancy + auth behavior — proves the DoD "tenancy + auth working" item.

Uses a throwaway in-memory SQLite DB so the test is self-contained and green.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.auth import JUNIOR_PREMIUM_CAP, can_approve
from core.common.enums import Role, Vertical
from core.models import Tenant
from core.tenancy.dependencies import get_ctx


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        s.add(Tenant(id="t-mga", name="MGA Co", vertical=Vertical.MGA))
        await s.commit()
        yield s
    await engine.dispose()


# ── authority helper (JUNIOR_PREMIUM_CAP) ────────────
def test_can_approve_authority_limits() -> None:
    assert can_approve(Role.JUNIOR, JUNIOR_PREMIUM_CAP - 1) is True
    assert can_approve(Role.JUNIOR, JUNIOR_PREMIUM_CAP + 1) is False  # → escalate
    assert can_approve(Role.SENIOR, JUNIOR_PREMIUM_CAP + 10_000) is True
    assert can_approve(Role.ADMIN, 10_000_000) is True


# ── vertical resolved from the tenant row ────────────
async def test_get_ctx_resolves_vertical_from_tenant(session: AsyncSession) -> None:
    ctx = await get_ctx(session, x_tenant_id="t-mga", x_user_id="u1", x_role="junior")
    assert ctx.vertical is Vertical.MGA
    assert ctx.tenant_id == "t-mga"
    assert ctx.role is Role.JUNIOR


# ── x-vertical fallback when tenant not seeded ───────
async def test_get_ctx_falls_back_to_header_vertical(session: AsyncSession) -> None:
    ctx = await get_ctx(
        session, x_tenant_id="unknown", x_user_id="u1", x_role="senior", x_vertical="ES"
    )
    assert ctx.vertical is Vertical.ES
    assert ctx.role is Role.SENIOR


# ── missing identity header → 401 ────────────────────
async def test_get_ctx_requires_tenant_header(session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc:
        await get_ctx(session, x_tenant_id=None, x_user_id="u1", x_role="junior")
    assert exc.value.status_code == 401


# ── unknown tenant + no fallback → 404 ───────────────
async def test_get_ctx_unknown_tenant_no_fallback(session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc:
        await get_ctx(session, x_tenant_id="ghost", x_user_id="u1", x_role="junior")
    assert exc.value.status_code == 404
