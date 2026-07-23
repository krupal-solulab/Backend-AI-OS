"""Frozen Pydantic DTOs — the data contracts every workflow passes between pipeline
steps. Deliberately vertical-agnostic: the same shapes carry MGA appetite results and
E&S carrier-matching results. These are transport/contract types, distinct from the
persisted SQLModel tables in ``core.models`` (e.g. the ``ExtractedModel`` DTO vs the
``ExtractedField`` table row)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.common.enums import (
    DecisionOutcome,
    DocumentKind,
    ReviewStatus,
    Role,
    RuleCheckType,
    Vertical,
)


class _Frozen(BaseModel):
    """Base for immutable, strictly-typed contract objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Ctx(_Frozen):
    """Request context resolved by tenancy + auth for every call."""

    tenant_id: str
    vertical: Vertical
    user_id: str
    role: Role


class WorkflowInput(_Frozen):
    """Entry payload for a pipeline run: a Nango email/doc bundle or a record id."""

    submission_id: str | None = None
    source_ref: str | None = None  # e.g. Nango message id or fixture path
    params: dict[str, Any] = Field(default_factory=dict)


class RawDocument(_Frozen):
    """A single raw document inside a RawBundle (pre-extraction)."""

    kind: DocumentKind
    filename: str
    content: str = ""
    uri: str | None = None


class RawBundle(_Frozen):
    """Output of ``ingest`` — raw email + attachments before extraction."""

    submission_id: str | None = None
    email_body: str | None = None
    documents: list[RawDocument] = Field(default_factory=list)


class Citation(_Frozen):
    """Grounding for an extracted value or an LLM claim — points at a source doc."""

    document_kind: DocumentKind
    filename: str
    locator: str | None = None  # line/section/cell reference


class ExtractedValue(_Frozen):
    """One cited, structured field extracted from the documents."""

    name: str
    value: Any
    confidence: float | None = None
    citation: Citation | None = None


class ExtractedModel(_Frozen):
    """Output of ``extract`` — the consolidated, cited structured field model
    (the DTO counterpart of the ``ExtractedField`` table rows)."""

    submission_id: str | None = None
    fields: list[ExtractedValue] = Field(default_factory=list)


class RuleResult(_Frozen):
    """Output of one rule evaluation (validation, appetite, or matching)."""

    rule_id: str
    check_type: RuleCheckType
    passed: bool
    score: float | None = None  # optional graded fit (e.g. E&S matching) alongside `passed`
    message: str | None = None
    citation: Citation | None = None


class Decision(_Frozen):
    """Output of the per-vertical Decision Core, normalized to a shared shape.
    MGA: appetite pass/fail → outcome + score. E&S: ranked carrier matches in
    ``details['matches']`` with a top-level outcome."""

    outcome: DecisionOutcome
    score: float | None = None
    rationale: str | None = None
    rule_results: list[RuleResult] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class Draft(_Frozen):
    """Output of ``draft`` — grounded, citation-enforced LLM text."""

    text: str
    citations: list[Citation] = Field(default_factory=list)


class OutputPackage(_Frozen):
    """Typed pipeline result surfaced to a human: recommendation + flags +
    missing-info + citations. Vertical-specific detail rides in ``payload``."""

    submission_id: str | None = None
    decision: Decision
    draft: Draft | None = None
    flags: list[str] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class ReviewItem(_Frozen):
    """An item awaiting a human in the review queue."""

    id: str
    submission_id: str | None = None
    workflow: str
    status: ReviewStatus = ReviewStatus.PENDING
    output: OutputPackage | None = None


class AuditEntry(_Frozen):
    """Immutable audit/decision log record (AI + human)."""

    actor: str  # "ai" | "human"
    who: str  # user id or system component
    what: str  # action / decision summary
    workflow: str
    tenant_id: str
    vertical: Vertical
    at: datetime | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
