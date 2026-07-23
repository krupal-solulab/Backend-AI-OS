"""The 12 shared base tables. See package docstring for portability notes."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, Column, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.types import JSON, DateTime
from sqlmodel import Field, SQLModel

from core.common.enums import (
    DecisionOutcome,
    DocumentKind,
    ReviewStatus,
    Role,
    RuleStatus,
    Vertical,
)


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _pk() -> object:
    return Field(default_factory=_uuid, primary_key=True)


def _enum_col(enum_type: type, *, nullable: bool = False) -> object:
    """VARCHAR + CHECK-backed enum column (portable across SQLite/Postgres)."""
    return Column(SAEnum(enum_type, native_enum=False, length=32), nullable=nullable)


def _ts_col(nullable: bool = False) -> Column:
    return Column(DateTime(timezone=True), nullable=nullable)


# ── 1. Tenant ────────────────────────────────────────
class Tenant(SQLModel, table=True):
    __tablename__ = "tenant"

    id: str = _pk()
    name: str = Field(sa_column=Column(String, nullable=False))
    vertical: Vertical = Field(sa_column=_enum_col(Vertical))
    junior_premium_cap: float | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 2. User ──────────────────────────────────────────
class User(SQLModel, table=True):
    __tablename__ = "user"

    id: str = _pk()
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    email: str = Field(sa_column=Column(String, nullable=False))
    name: str | None = None
    role: Role = Field(sa_column=_enum_col(Role))
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 3. Submission ────────────────────────────────────
class Submission(SQLModel, table=True):
    __tablename__ = "submission"

    id: str = _pk()
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    vertical: Vertical = Field(sa_column=_enum_col(Vertical))
    external_ref: str | None = None
    subject: str | None = None
    status: str = Field(default="received", sa_column=Column(String, nullable=False))
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 4. Document ──────────────────────────────────────
class Document(SQLModel, table=True):
    __tablename__ = "document"

    id: str = _pk()
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    kind: DocumentKind = Field(sa_column=_enum_col(DocumentKind))
    filename: str = Field(sa_column=Column(String, nullable=False))
    uri: str | None = None
    content: str | None = None
    classification_confidence: float | None = None
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 5. ExtractedField ────────────────────────────────
class ExtractedField(SQLModel, table=True):
    __tablename__ = "extracted_field"

    id: str = _pk()
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    document_id: str | None = Field(
        default=None, sa_column=Column(String, ForeignKey("document.id"), nullable=True)
    )
    name: str = Field(sa_column=Column(String, nullable=False))
    value: str | None = None
    confidence: float | None = None
    citation: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 6. RuleSet ───────────────────────────────────────
class RuleSet(SQLModel, table=True):
    __tablename__ = "rule_set"

    id: str = _pk()
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    vertical: Vertical = Field(sa_column=_enum_col(Vertical))
    key: str = Field(sa_column=Column(String, nullable=False))  # e.g. "acord_validation"
    name: str | None = None
    active_version_id: str | None = None
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 7. RuleVersion (its OWN table) ───────────────────
class RuleVersion(SQLModel, table=True):
    __tablename__ = "rule_version"

    id: str = _pk()
    rule_set_id: str = Field(
        sa_column=Column(String, ForeignKey("rule_set.id"), nullable=False)
    )
    version: int = Field(default=1)
    status: RuleStatus = Field(sa_column=_enum_col(RuleStatus))
    rules: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    published_at: datetime | None = Field(default=None, sa_column=_ts_col(nullable=True))
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 8. Decision ──────────────────────────────────────
class Decision(SQLModel, table=True):
    __tablename__ = "decision"

    id: str = _pk()
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    outcome: DecisionOutcome = Field(sa_column=_enum_col(DecisionOutcome))
    score: float | None = None
    rationale: str | None = None
    details: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 9. OutputPackage ─────────────────────────────────
class OutputPackage(SQLModel, table=True):
    __tablename__ = "output_package"

    id: str = _pk()
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    decision_id: str | None = Field(
        default=None, sa_column=Column(String, ForeignKey("decision.id"), nullable=True)
    )
    workflow: str = Field(sa_column=Column(String, nullable=False))
    payload: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 10. ReviewItem ───────────────────────────────────
class ReviewItem(SQLModel, table=True):
    __tablename__ = "review_item"

    id: str = _pk()
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str | None = Field(
        default=None, sa_column=Column(String, ForeignKey("submission.id"), nullable=True)
    )
    output_package_id: str | None = Field(
        default=None, sa_column=Column(String, ForeignKey("output_package.id"), nullable=True)
    )
    workflow: str = Field(sa_column=Column(String, nullable=False))
    status: ReviewStatus = Field(sa_column=_enum_col(ReviewStatus))
    assigned_to: str | None = None
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 11. AuditEntry ───────────────────────────────────
class AuditEntry(SQLModel, table=True):
    __tablename__ = "audit_entry"

    id: str = _pk()
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    vertical: Vertical = Field(sa_column=_enum_col(Vertical))
    actor: str = Field(sa_column=Column(String, nullable=False))  # "ai" | "human"
    who: str = Field(sa_column=Column(String, nullable=False))
    what: str = Field(sa_column=Column(String, nullable=False))
    workflow: str = Field(sa_column=Column(String, nullable=False))
    detail: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 12. Connection (Nango) ───────────────────────────
class Connection(SQLModel, table=True):
    __tablename__ = "connection"
    __table_args__ = (CheckConstraint("provider <> ''", name="ck_connection_provider"),)

    id: str = _pk()
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    provider: str = Field(sa_column=Column(String, nullable=False))  # google-mail | ...
    nango_connection_id: str | None = None
    status: str = Field(default="disconnected", sa_column=Column(String, nullable=False))
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())


# ── 13. JobRun (Phase 1 — Arq job tracking + queryable error queue) ──
class JobRun(SQLModel, table=True):
    __tablename__ = "job_run"

    id: str = _pk()
    tenant_id: str | None = Field(
        default=None, sa_column=Column(String, ForeignKey("tenant.id"), nullable=True)
    )
    job_name: str = Field(sa_column=Column(String, nullable=False))
    # status values: queued | running | success | error (see core.jobs.JobStatus)
    status: str = Field(default="queued", sa_column=Column(String, nullable=False, index=True))
    submission_id: str | None = None
    args: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    result: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    error: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    attempts: int = Field(default=0)
    created_at: datetime = Field(default_factory=_now, sa_column=_ts_col())
    updated_at: datetime = Field(default_factory=_now, sa_column=_ts_col())
