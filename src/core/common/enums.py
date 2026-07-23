"""Shared enums (contracts). String-backed so they persist portably to SQLite and
Postgres alike (no PG-native ENUM type)."""

from __future__ import annotations

from enum import StrEnum


class Vertical(StrEnum):
    """The tenant's business line — selects decision core / rules / workflows."""

    MGA = "MGA"
    ES = "ES"


class Role(StrEnum):
    """RBAC roles (shared across both verticals)."""

    JUNIOR = "junior"
    SENIOR = "senior"
    ADMIN = "admin"


class DocumentKind(StrEnum):
    """Classification of an ingested document; inferred from filename in fixtures."""

    ACORD = "acord"
    LOSS_RUN = "loss_run"
    FINANCIALS = "financials"
    SOV = "sov"
    EMAIL = "email"
    OTHER = "other"


class RuleCheckType(StrEnum):
    """The 6 check types the generic rules evaluator supports (Phase 1)."""

    REQUIRED = "required"
    REGEX = "regex"
    MIN = "min"
    MAX = "max"
    COMPARE = "compare"
    CROSS_DOC = "crossDoc"


class RuleStatus(StrEnum):
    """Lifecycle of a rule-set version (publish / rollback)."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ROLLED_BACK = "rolled_back"


class DecisionOutcome(StrEnum):
    """Vertical-agnostic decision outcome. MGA maps appetite → these; E&S maps a
    ranked carrier match into PROCEED / REQUEST_INFO / DECLINE at the top level."""

    PROCEED = "PROCEED"
    REQUEST_INFO = "REQUEST_INFO"
    DECLINE = "DECLINE"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    OVERRIDDEN = "overridden"
    ESCALATED = "escalated"
    SENT = "sent"
    ISSUED = "issued"


class ReviewAction(StrEnum):
    APPROVE = "approve"
    OVERRIDE = "override"
    ESCALATE = "escalate"
    SEND = "send"
    ISSUE = "issue"
