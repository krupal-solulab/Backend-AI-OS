"""RBAC policy: role guards + authority helper."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, status

from core.common.dtos import Ctx
from core.common.enums import Role
from core.config import get_settings
from core.tenancy.dependencies import get_ctx

JUNIOR_PREMIUM_CAP: float = get_settings().junior_premium_cap


def can_approve(role: Role, amount: float | None = None, *, cap: float | None = None) -> bool:
    """Authority check. Admin/senior may approve any amount. A junior may approve only
    up to ``cap`` (defaults to ``JUNIOR_PREMIUM_CAP``); anything above → escalate."""
    if role in (Role.ADMIN, Role.SENIOR):
        return True
    if role is Role.JUNIOR:
        if amount is None:
            return True
        return amount <= (cap if cap is not None else JUNIOR_PREMIUM_CAP)
    return False


def require_roles(*allowed: Role) -> Callable[[Ctx], Awaitable[Ctx]]:
    """Build a FastAPI dependency that admits only the given roles."""

    async def _guard(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        if ctx.role not in allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role '{ctx.role}' not permitted; requires one of "
                f"{[r.value for r in allowed]}",
            )
        return ctx

    return _guard


def require_role(role: Role) -> Callable[[Ctx], Awaitable[Ctx]]:
    """Convenience guard for a single required role."""
    return require_roles(role)
