"""LLM layer (Phase 1) — a grounded, citation-enforced ``LLMService`` behind a provider
interface so the model/provider stays a config choice.

Contract (CORE_MODULES.md, non-negotiable): receive only extracted facts, cite the
source for each claim, never fabricate. Enforced in the wrapper via citation
post-validation. ``MockLLMProvider`` lets the smoke test run with no API key.
"""

from core.llm.service import (
    LLMProvider,
    LLMService,
    MockLLMProvider,
    OpenAIProvider,
    build_llm_service,
)

__all__ = [
    "LLMProvider",
    "LLMService",
    "MockLLMProvider",
    "OpenAIProvider",
    "build_llm_service",
]
