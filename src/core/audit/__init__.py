"""Audit (Phase 1) — immutable decision/audit log (AI + human).

Every decision and human action is appended here; this log is the source that
Governance & Portfolio reporting reads. Records are append-only (no update/delete API).
"""

from core.audit.service import AuditService, DefaultAuditService

__all__ = ["AuditService", "DefaultAuditService"]
