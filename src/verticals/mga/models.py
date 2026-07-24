"""MGA-vertical tables (additive; extend the shared base, never modify it).

``mga_appetite_result`` persists the Appetite Engine's output per submission;
``mga_renewal_result`` persists the Renewal Comparison Engine's output — both so Phase-4
Governance & Portfolio can read decision history. Portable types only (String/JSON),
same conventions as the shared base tables.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Column, ForeignKey, String
from sqlalchemy.types import JSON, DateTime
from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class MgaAppetiteResult(SQLModel, table=True):
    __tablename__ = "mga_appetite_result"

    id: str = Field(default_factory=_uuid, primary_key=True)
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    outcome: str = Field(sa_column=Column(String, nullable=False, index=True))
    score: float | None = Field(default=None)
    triggered_rule_ids: list | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    flags: list | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class MgaRenewalResult(SQLModel, table=True):
    __tablename__ = "mga_renewal_result"

    id: str = Field(default_factory=_uuid, primary_key=True)
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    recommendation: str = Field(sa_column=Column(String, nullable=False, index=True))
    outcome: str = Field(sa_column=Column(String, nullable=False))
    score: float | None = Field(default=None)
    retention: str | None = Field(default=None)
    triggered_rule_ids: list | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    change_flags: list | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
