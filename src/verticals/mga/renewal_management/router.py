"""Routes under /api/mga/renewal — list, detail, run, act. Human-in-the-loop; no auto-send."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import Ctx
from core.db import get_session
from core.review_queue import AuthorityError
from core.tenancy import get_ctx
from verticals.mga.renewal_management.schema import ActRequest, RenewalDetail, RenewalRow
from verticals.mga.renewal_management.service import RenewalService

router = APIRouter(prefix="/renewal", tags=["mga:renewal-management"])
_service = RenewalService()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CtxDep = Annotated[Ctx, Depends(get_ctx)]


@router.get("", response_model=list[RenewalRow])
async def list_renewals(session: SessionDep, ctx: CtxDep) -> list[RenewalRow]:
    return await _service.list_rows(session, ctx)


@router.post("/run", response_model=RenewalDetail)
async def run_renewal(session: SessionDep, ctx: CtxDep, message_id: str) -> RenewalDetail:
    """Dev/ingestion trigger: run the renewal comparison for one fixture case and persist."""
    return await _service.renew(session, ctx, message_id)


@router.get("/{submission_id}", response_model=RenewalDetail)
async def get_renewal(session: SessionDep, ctx: CtxDep, submission_id: str) -> RenewalDetail:
    detail = await _service.get_detail(session, ctx, submission_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "renewal not found")
    return detail


@router.post("/{submission_id}/act")
async def act_on_renewal(
    session: SessionDep, ctx: CtxDep, submission_id: str, body: ActRequest
) -> dict[str, str]:
    """Human action: approve | send (broker outreach) | escalate."""
    try:
        return await _service.act(session, ctx, submission_id, body.action, body.amount)
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
