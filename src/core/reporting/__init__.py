"""Reporting (Phase 1) — a small aggregation/rollup framework over the audit log +
entities. Group-by counts by a chosen dimension; the foundation Governance & Portfolio
(Phase 4) build on. Tenant-scoped.
"""

from core.reporting.service import DefaultReportingService, ReportingService

__all__ = ["DefaultReportingService", "ReportingService"]
