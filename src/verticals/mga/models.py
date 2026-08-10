"""MGA-vertical tables (additive; extend the shared base, never modify it).

``mga_appetite_result`` persists the Appetite Engine's output per submission;
``mga_renewal_result`` persists the Renewal Comparison Engine's output;
``mga_broker_comm_result`` persists the Broker Communication Copilot's drafting output;
``mga_endorsement_result`` persists the Endorsement Processing engine's output, including
the MEP-05 PAS write-back record Bordereau Reporting's completeness check depends on;
``mga_quoting_result`` persists the Quoting & Rating Support engine's worksheet output;
``mga_bind_result`` persists the Bind Order & Issuance engine's output, including the
MBI-04 PAS write-back and MBI-05 issuance reconciliation status; ``mga_governance_result``
persists the Appetite Governance & Audit Trail engine's output — the aggregation layer
that reads the decision history the other tables here provide; ``mga_portfolio_result``
persists the Portfolio & Book Performance Reporting engine's output — the final MGA
roadmap workflow, itself an aggregation layer over every other workflow's decision
history plus Appetite Governance's own AG-06 concentration findings. Portable types only
(String/JSON), same conventions as the shared base tables.
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


class MgaBrokerCommResult(SQLModel, table=True):
    __tablename__ = "mga_broker_comm_result"

    id: str = Field(default_factory=_uuid, primary_key=True)
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    source_workflow: str = Field(sa_column=Column(String, nullable=False))
    comm_type: str = Field(sa_column=Column(String, nullable=False, index=True))
    tone: str | None = Field(default=None)
    requires_compliance_review: bool = Field(default=False)
    sensitive: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class MgaEndorsementResult(SQLModel, table=True):
    __tablename__ = "mga_endorsement_result"

    id: str = Field(default_factory=_uuid, primary_key=True)
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    classification: str = Field(sa_column=Column(String, nullable=False))
    outcome: str = Field(sa_column=Column(String, nullable=False, index=True))
    premium_impact: float | None = Field(default=None)
    resulting_total_premium: float | None = Field(default=None)
    excluded_class_matched: str | None = Field(default=None)
    carrier_referral_drafted: bool = Field(default=False)
    write_back_logged: bool = Field(default=False)
    bordereau_schema_validated: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class MgaQuotingResult(SQLModel, table=True):
    __tablename__ = "mga_quoting_result"

    id: str = Field(default_factory=_uuid, primary_key=True)
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    class_code: str = Field(sa_column=Column(String, nullable=False))
    status: str = Field(sa_column=Column(String, nullable=False, index=True))
    total_indicated_premium: float | None = Field(default=None)
    benchmark_flagged: bool = Field(default=False)
    any_adjustment_capped: bool = Field(default=False)
    any_minimum_applied: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class MgaBindResult(SQLModel, table=True):
    __tablename__ = "mga_bind_result"

    id: str = Field(default_factory=_uuid, primary_key=True)
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    status: str = Field(sa_column=Column(String, nullable=False, index=True))
    authority_outcome: str | None = Field(default=None)
    write_back_logged: bool = Field(default=False)
    issuance_status: str = Field(sa_column=Column(String, nullable=False))
    issuance_discrepancy_count: int = Field(default=0)
    post_bind_obligation_count: int = Field(default=0)
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class MgaGovernanceResult(SQLModel, table=True):
    __tablename__ = "mga_governance_result"

    id: str = Field(default_factory=_uuid, primary_key=True)
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    status: str = Field(sa_column=Column(String, nullable=False, index=True))
    gap_count: int = Field(default=0)
    flagged_finding_count: int = Field(default=0)
    has_audit_report: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class MgaPortfolioResult(SQLModel, table=True):
    __tablename__ = "mga_portfolio_result"

    id: str = Field(default_factory=_uuid, primary_key=True)
    tenant_id: str = Field(sa_column=Column(String, ForeignKey("tenant.id"), nullable=False))
    submission_id: str = Field(
        sa_column=Column(String, ForeignKey("submission.id"), nullable=False)
    )
    status: str = Field(sa_column=Column(String, nullable=False, index=True))
    completeness_status: str = Field(sa_column=Column(String, nullable=False))
    gap_count: int = Field(default=0)
    has_loss_ratio: bool = Field(default=False)
    has_renewal_retention: bool = Field(default=False)
    has_appetite_exposure: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
