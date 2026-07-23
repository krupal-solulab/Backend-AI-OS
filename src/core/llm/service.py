"""LLMService wrapper + providers (OpenAI + mock) + citation post-validation."""

from __future__ import annotations

from typing import Protocol

from core.common.dtos import Citation, Ctx, Draft, ExtractedValue
from core.config import Settings, get_settings

# Tier → settings attribute holding the model id.
_TIER_ATTR = {
    "fast": "llm_model_fast",
    "standard": "llm_model_standard",
    "deep": "llm_model_deep",
}

_SYSTEM_PROMPT = (
    "You are an insurance operations assistant. Use ONLY the facts provided. "
    "Cite the source document for every claim. If a fact is not provided, write "
    "'not available in submitted documents' — never fabricate."
)


def _model_for_tier(settings: Settings, tier: str) -> str:
    return str(getattr(settings, _TIER_ATTR.get(tier, "llm_model_standard")))


def _facts_block(facts: list[ExtractedValue]) -> str:
    lines = []
    for f in facts:
        src = ""
        if f.citation is not None:
            src = f" [source: {f.citation.filename}"
            src += f", {f.citation.locator}]" if f.citation.locator else "]"
        lines.append(f"- {f.name}: {f.value}{src}")
    return "\n".join(lines)


class LLMProvider(Protocol):
    async def complete(self, *, model: str, system: str, user: str) -> str: ...


class MockLLMProvider:
    """Deterministic, offline provider — echoes the grounded facts. No network, no key."""

    async def complete(self, *, model: str, system: str, user: str) -> str:
        return f"[mock:{model}] Draft grounded in provided facts.\n{user}"


class OpenAIProvider:
    """Thin wrapper over the official ``openai`` async SDK. Client is created lazily so
    importing this module never requires a key."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def complete(self, *, model: str, system: str, user: str) -> str:
        client = self._get_client()
        resp = await client.chat.completions.create(  # type: ignore[attr-defined]
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
        )
        return resp.choices[0].message.content or ""


class LLMService:
    """Grounded drafting with model-tier routing + citation post-validation."""

    def __init__(self, provider: LLMProvider, settings: Settings | None = None) -> None:
        self._provider = provider
        self._settings = settings or get_settings()

    async def draft(
        self,
        ctx: Ctx,
        prompt: str,
        facts: list[ExtractedValue],
        *,
        tier: str = "standard",
    ) -> Draft:
        model = _model_for_tier(self._settings, tier)
        user = f"{prompt}\n\nFACTS:\n{_facts_block(facts)}"
        text = await self._provider.complete(model=model, system=_SYSTEM_PROMPT, user=user)

        # Citations are the sources of the facts we actually grounded on.
        citations = [f.citation for f in facts if f.citation is not None]
        self._validate_citations(citations, facts)
        return Draft(text=text, citations=citations)

    @staticmethod
    def _validate_citations(citations: list[Citation], facts: list[ExtractedValue]) -> None:
        """Every returned citation must trace to a provided fact — no fabricated sources."""
        allowed = {
            (f.citation.filename, f.citation.locator)
            for f in facts
            if f.citation is not None
        }
        for c in citations:
            if (c.filename, c.locator) not in allowed:
                raise ValueError(f"fabricated citation not grounded in facts: {c.filename}")


def build_llm_service(settings: Settings | None = None) -> LLMService:
    """Factory: real OpenAI provider when a key is configured, else the mock."""
    settings = settings or get_settings()
    provider: LLMProvider
    if settings.openai_api_key and settings.openai_api_key != "sk-...":
        provider = OpenAIProvider(settings.openai_api_key)
    else:
        provider = MockLLMProvider()
    return LLMService(provider, settings)
