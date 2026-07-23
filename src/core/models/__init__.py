"""SQLModel table definitions — the 12 shared base entities (Phase 0).

Portable by design: enums are stored as VARCHAR + CHECK (``native_enum=False``) and
structured blobs use SQLAlchemy's generic ``JSON`` type, so the identical models run on
SQLite (dev) and Postgres (later) with no change. Alembic owns the schema.
"""

from __future__ import annotations

from core.models.tables import (
    AuditEntry,
    Connection,
    Decision,
    Document,
    ExtractedField,
    JobRun,
    OutputPackage,
    ReviewItem,
    RuleSet,
    RuleVersion,
    Submission,
    Tenant,
    User,
)

__all__ = [
    "AuditEntry",
    "Connection",
    "Decision",
    "Document",
    "ExtractedField",
    "JobRun",
    "OutputPackage",
    "ReviewItem",
    "RuleSet",
    "RuleVersion",
    "Submission",
    "Tenant",
    "User",
]
