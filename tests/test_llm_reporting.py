"""LLM grounding/citation validation + reporting rollup."""

from __future__ import annotations

import pytest

from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Citation, ExtractedValue
from core.common.enums import DocumentKind
from core.config import Settings
from core.llm import LLMService, MockLLMProvider, build_llm_service
from core.reporting import DefaultReportingService


def _fact(name: str, cite: bool = True) -> ExtractedValue:
    c = Citation(document_kind=DocumentKind.ACORD, filename="acord_application.txt",
                 locator="line 3") if cite else None
    return ExtractedValue(name=name, value="X", citation=c)


async def test_mock_llm_draft_is_grounded_and_cited(mga_ctx) -> None:
    llm = build_llm_service(Settings(openai_api_key=""))  # forces mock
    facts = [_fact("acord.named_insured"), _fact("acord.fein")]
    draft = await llm.draft(mga_ctx, "Summarize.", facts, tier="fast")
    assert draft.text.startswith("[mock:")
    assert len(draft.citations) == 2
    assert all(c.filename == "acord_application.txt" for c in draft.citations)


async def test_llm_rejects_fabricated_citation(mga_ctx) -> None:
    class FabricatingService(LLMService):
        async def draft(self, ctx, prompt, facts, *, tier="standard"):  # type: ignore[override]
            bogus = [Citation(document_kind=DocumentKind.OTHER, filename="ghost.pdf")]
            self._validate_citations(bogus, facts)  # should raise
            return await super().draft(ctx, prompt, facts, tier=tier)

    svc = FabricatingService(MockLLMProvider(), Settings())
    with pytest.raises(ValueError, match="fabricated citation"):
        await svc.draft(mga_ctx, "x", [_fact("acord.fein")])


async def test_reporting_rollup_by_workflow(mem_session, mga_ctx) -> None:
    audit = DefaultAuditService()
    for wf in ("submission_triage", "submission_triage", "renewal"):
        await audit.record(
            mem_session, mga_ctx,
            AuditEntry(actor="ai", who="system", what="decision", workflow=wf,
                       tenant_id=mga_ctx.tenant_id, vertical=mga_ctx.vertical),
        )
    rollup = await DefaultReportingService().rollup(mem_session, mga_ctx, "workflow")
    assert rollup["total"] == 3
    assert rollup["counts"]["submission_triage"] == 2
    assert rollup["counts"]["renewal"] == 1


async def test_reporting_rejects_unknown_dimension(mem_session, mga_ctx) -> None:
    with pytest.raises(ValueError, match="unknown dimension"):
        await DefaultReportingService().rollup(mem_session, mga_ctx, "not_a_dim")
