"""Routes under /api/mga/endorsement-processing — list, detail, run (process a change
request), act. Human-in-the-loop; no auto-processing, no auto-referral outcome."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import Ctx
from core.db import get_session
from core.review_queue import AuthorityError
from core.tenancy import get_ctx
from verticals.mga.endorsement_processing.schema import (
    ActRequest,
    EndorsementDetail,
    EndorsementRow,
)
from verticals.mga.endorsement_processing.service import EndorsementService

router = APIRouter(prefix="/endorsement-processing", tags=["mga:endorsement-processing"])
_service = EndorsementService()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CtxDep = Annotated[Ctx, Depends(get_ctx)]


@router.get("", response_model=list[EndorsementRow])
async def list_endorsements(session: SessionDep, ctx: CtxDep) -> list[EndorsementRow]:
    return await _service.list_rows(session, ctx)


@router.post("/run", response_model=EndorsementDetail)
async def run_endorsement(session: SessionDep, ctx: CtxDep, scenario: str) -> EndorsementDetail:
    """Dev/ingestion trigger: process one fixture scenario (or dataset scenario name)."""
    try:
        return await _service.process(session, ctx, scenario)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/{submission_id}", response_model=EndorsementDetail)
async def get_endorsement(
    session: SessionDep, ctx: CtxDep, submission_id: str
) -> EndorsementDetail:
    detail = await _service.get_detail(session, ctx, submission_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "endorsement not found")
    return detail


@router.post("/{submission_id}/act")
async def act_on_endorsement(
    session: SessionDep, ctx: CtxDep, submission_id: str, body: ActRequest
) -> dict[str, str]:
    """Human action: approve (issue) | send (carrier referral) | escalate."""
    try:
        return await _service.act(session, ctx, submission_id, body.action)
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
