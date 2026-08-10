"""Routes under /api/mga/broker-copilot — list, detail, draft (trigger), act. Human-in-
the-loop; no auto-send."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import Ctx
from core.db import get_session
from core.review_queue import AuthorityError
from core.tenancy import get_ctx
from verticals.mga.broker_copilot.schema import ActRequest, CommDraft, DraftRequest
from verticals.mga.broker_copilot.service import BrokerCopilotService

router = APIRouter(prefix="/broker-copilot", tags=["mga:broker-copilot"])
_service = BrokerCopilotService()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CtxDep = Annotated[Ctx, Depends(get_ctx)]


@router.get("", response_model=list[CommDraft])
async def list_drafts(session: SessionDep, ctx: CtxDep) -> list[CommDraft]:
    return await _service.list_drafts(session, ctx)


@router.post("/draft", response_model=CommDraft)
async def draft_communication(session: SessionDep, ctx: CtxDep, body: DraftRequest) -> CommDraft:
    """Generate a draft FROM an existing Submission Triage or Renewal Management decision."""
    try:
        return await _service.draft(session, ctx, body.source_workflow, body.submission_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/{draft_id}", response_model=CommDraft)
async def get_draft(session: SessionDep, ctx: CtxDep, draft_id: str) -> CommDraft:
    detail = await _service.get_detail(session, ctx, draft_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "draft not found")
    return detail


@router.post("/{submission_id}/act")
async def act_on_draft(
    session: SessionDep, ctx: CtxDep, submission_id: str, body: ActRequest
) -> dict[str, str]:
    """Human action: approve | send (broker outreach) | escalate."""
    try:
        return await _service.act(session, ctx, submission_id, body.action, body.body)
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
