"""Tenancy — resolves the request Ctx (tenant + vertical + user + role).

Phase 0 STUB: identity comes from headers (``x-tenant-id`` / ``x-user-id`` /
``x-role``); there is no login flow. The ``vertical`` is resolved by looking up the
Tenant row, falling back to an ``x-vertical`` header when the tenant isn't seeded.
"""

from core.tenancy.dependencies import get_ctx

__all__ = ["get_ctx"]
