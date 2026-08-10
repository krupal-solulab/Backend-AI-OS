"""Routes under /api/mga/quoting-rating — list, detail, run (process a rating request),
act. Human reviews the worksheet and finalizes manually; no auto-finalization."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import Ctx
from core.db import get_session
from core.review_queue import AuthorityError
from core.tenancy import get_ctx
from verticals.mga.quoting_rating.schema import ActRequest, WorksheetDetail, WorksheetRow
from verticals.mga.quoting_rating.service import QuotingService

router = APIRouter(prefix="/quoting-rating", tags=["mga:quoting-rating"])
_service = QuotingService()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CtxDep = Annotated[Ctx, Depends(get_ctx)]


@router.get("", response_model=list[WorksheetRow])
async def list_worksheets(session: SessionDep, ctx: CtxDep) -> list[WorksheetRow]:
    return await _service.list_rows(session, ctx)


@router.post("/run", response_model=WorksheetDetail)
async def run_worksheet(session: SessionDep, ctx: CtxDep, scenario: str) -> WorksheetDetail:
    """Dev/ingestion trigger: calculate a worksheet for one fixture scenario."""
    try:
        return await _service.process(session, ctx, scenario)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/{submission_id}", response_model=WorksheetDetail)
async def get_worksheet(
    session: SessionDep, ctx: CtxDep, submission_id: str
) -> WorksheetDetail:
    detail = await _service.get_detail(session, ctx, submission_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "worksheet not found")
    return detail


@router.post("/{submission_id}/act")
async def act_on_worksheet(
    session: SessionDep, ctx: CtxDep, submission_id: str, body: ActRequest
) -> dict[str, str]:
    """Human action: approve (finalize) | send (broker) | escalate."""
    try:
        return await _service.act(session, ctx, submission_id, body.action)
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
