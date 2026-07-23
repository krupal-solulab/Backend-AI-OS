"""Auth — RBAC roles, authority limits (junior premium cap), and route guards.

Phase 0: identity is stubbed by tenancy (header-based). This module supplies the
policy layer — role gating and the ``can_approve`` authority helper — that workflows
and the review queue reuse rather than re-implement.
"""

from core.auth.policy import (
    JUNIOR_PREMIUM_CAP,
    can_approve,
    require_role,
    require_roles,
)

__all__ = [
    "JUNIOR_PREMIUM_CAP",
    "can_approve",
    "require_role",
    "require_roles",
]
