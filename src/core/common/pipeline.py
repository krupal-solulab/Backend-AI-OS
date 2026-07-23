"""The frozen ``WorkflowPipeline`` Protocol — the interface every workflow implements,
in EITHER vertical. It is deliberately vertical-agnostic: ``decide`` returns the shared
``Decision`` shape whether the vertical runs an MGA appetite engine or an E&S
matching/ranking engine. Most steps just call shared services; the only real
per-workflow code is ``decide`` (which rule sets to run + how to map results) and
``package`` (its output shape).

See WORKFLOW_TEMPLATE.md. Changing this Protocol is a reviewed contract event."""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from core.common.dtos import (
    Ctx,
    Decision,
    Draft,
    ExtractedModel,
    OutputPackage,
    RawBundle,
    RuleResult,
    WorkflowInput,
)

Out = TypeVar("Out", bound=OutputPackage, covariant=True)


@runtime_checkable
class WorkflowPipeline(Protocol[Out]):
    """The 7-step pipeline every workflow follows (steps 8+ — queue, human action,
    write-back, audit — are handled by shared services, not this Protocol)."""

    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle:
        """Pull the broker email + attachments (Nango) or resolve a record id."""
        ...

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        """Classify + extract a cited structured field model (via ExtractionService)."""
        ...

    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]:
        """Run validation rule sets over the extracted data (via RulesEngine)."""
        ...

    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision:
        """Per-vertical Decision Core: MGA appetite OR E&S matching/ranking."""
        ...

    async def draft(self, ctx: Ctx, decision: Decision) -> Draft:
        """Grounded, citation-enforced narrative / recommendation (via LLMService)."""
        ...

    async def package(self, ctx: Ctx, *args: object) -> Out:
        """Assemble the typed OutputPackage surfaced to the review queue."""
        ...
