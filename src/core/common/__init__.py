"""core.common — FROZEN CONTRACTS (Phase 0.4).

Every workflow, in either vertical, depends on the DTOs, enums, and the
``WorkflowPipeline`` Protocol exported here — never on a concrete implementation.
Changing anything in this package is a deliberate, reviewed event (see PARALLEL_WORK.md).
"""

from __future__ import annotations

from core.common.dtos import (
    AuditEntry,
    Ctx,
    Decision,
    Draft,
    ExtractedModel,
    OutputPackage,
    RawBundle,
    ReviewItem,
    RuleResult,
    WorkflowInput,
)
from core.common.enums import (
    DecisionOutcome,
    DocumentKind,
    ReviewAction,
    ReviewStatus,
    Role,
    RuleCheckType,
    RuleStatus,
    Vertical,
)
from core.common.pipeline import WorkflowPipeline

__all__ = [
    "AuditEntry",
    "Ctx",
    "Decision",
    "DecisionOutcome",
    "DocumentKind",
    "Draft",
    "ExtractedModel",
    "OutputPackage",
    "RawBundle",
    "ReviewAction",
    "ReviewItem",
    "ReviewStatus",
    "Role",
    "RuleCheckType",
    "RuleResult",
    "RuleStatus",
    "Vertical",
    "WorkflowInput",
    "WorkflowPipeline",
]
