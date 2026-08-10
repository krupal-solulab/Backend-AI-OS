"""Routes under /api/mga/appetite-governance — list, detail, run (process an audit
period), act. This workflow makes no underwriting decisions; every finding routes to a
human-reviewed governance suggestion queue, never an automated action."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import Ctx
from core.db import get_session
from core.review_queue import AuthorityError
from core.tenancy import get_ctx
from verticals.mga.appetite_governance.schema import ActRequest, GovernanceDetail, GovernanceRow
from verticals.mga.appetite_governance.service import GovernanceService

router = APIRouter(prefix="/appetite-governance", tags=["mga:appetite-governance"])
_service = GovernanceService()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CtxDep = Annotated[Ctx, Depends(get_ctx)]


@router.get("", response_model=list[GovernanceRow])
async def list_audits(session: SessionDep, ctx: CtxDep) -> list[GovernanceRow]:
    return await _service.list_rows(session, ctx)


@router.post("/run", response_model=GovernanceDetail)
async def run_audit(session: SessionDep, ctx: CtxDep, scenario: str) -> GovernanceDetail:
    """Dev/ingestion trigger: process one fixture scenario (an audit period, a rule
    version change, override events, an external audit request, or a portfolio
    concentration snapshot)."""
    try:
        return await _service.process(session, ctx, scenario)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/{submission_id}", response_model=GovernanceDetail)
async def get_audit(session: SessionDep, ctx: CtxDep, submission_id: str) -> GovernanceDetail:
    detail = await _service.get_detail(session, ctx, submission_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "governance analysis not found")
    return detail


@router.post("/{submission_id}/act")
async def act_on_audit(
    session: SessionDep, ctx: CtxDep, submission_id: str, body: ActRequest
) -> dict[str, str]:
    """Human action: approve (mark reviewed) | send (submit audit report externally) |
    escalate."""
    try:
        return await _service.act(session, ctx, submission_id, body.action)
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
