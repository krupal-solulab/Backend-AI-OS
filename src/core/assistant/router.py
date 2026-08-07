"""Routes under ``/api/core/assistant`` — one chat surface grounded in real,
live cross-workflow data (see ``service.py``). Reuses the same
citation-enforced ``LLMService.draft()`` every workflow's own drafting step
already goes through — nothing here bypasses the "cite it or say it's not
available" contract.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.assistant.service import gather_context, gather_overview
from core.common.dtos import Ctx
from core.db import get_session
from core.llm import build_llm_service
from core.tenancy.dependencies import get_ctx

router = APIRouter(prefix="/assistant", tags=["core:assistant"])

CtxDep = Annotated[Ctx, Depends(get_ctx)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


class CitationOut(BaseModel):
    label: str
    item_id: str


class ChatResponse(BaseModel):
    text: str
    citations: list[CitationOut]
    grounded_fact_count: int


class OverviewRecentOut(BaseModel):
    workflow: str
    ref: str


class OverviewOut(BaseModel):
    pending_counts: dict[str, int]
    recent: list[OverviewRecentOut]


_SYSTEM_FRAMING = (
    "You are answering a placement-team user's question about their real, live "
    "book of business across all 10 E&S workflows. Use ONLY the FACTS given "
    "below — each fact is one real review item from one real workflow, with "
    "its own real submission reference. If nothing relevant was provided, say "
    "so plainly and suggest what to ask instead — never invent a submission, "
    "carrier, dollar figure, or status that isn't in the facts."
)


@router.post("/chat")
async def chat(body: ChatRequest, ctx: CtxDep, session: SessionDep) -> ChatResponse:
    facts = await gather_context(session, ctx.tenant_id, body.message)

    history_block = "\n".join(f"{m.role}: {m.content}" for m in body.history[-10:])
    prompt = (
        f"{_SYSTEM_FRAMING}\n\n"
        + (f"Conversation so far:\n{history_block}\n\n" if history_block else "")
        + f"New question: {body.message}"
    )

    llm = build_llm_service()
    draft = await llm.draft(ctx, prompt, facts, tier="standard")

    seen: set[str] = set()
    citations: list[CitationOut] = []
    for c in draft.citations:
        if c.filename in seen:
            continue
        seen.add(c.filename)
        citations.append(CitationOut(label=c.filename, item_id=c.locator or ""))
    return ChatResponse(text=draft.text, citations=citations, grounded_fact_count=len(facts))


@router.get("/context")
async def context(ctx: CtxDep, session: SessionDep) -> OverviewOut:
    overview = await gather_overview(session, ctx.tenant_id)
    return OverviewOut(
        pending_counts=overview["pending_counts"],
        recent=[OverviewRecentOut(**r) for r in overview["recent"]],
    )
