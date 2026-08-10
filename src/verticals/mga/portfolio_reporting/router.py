"""Routes under /api/mga/portfolio-reporting — list, detail, run (generate a report),
act. Like Appetite Governance & Audit Trail, this workflow makes no underwriting
decisions; every report routes to a human-reviewed queue before being sent externally."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import Ctx
from core.db import get_session
from core.review_queue import AuthorityError
from core.tenancy import get_ctx
from verticals.mga.portfolio_reporting.schema import (
    ActRequest,
    PortfolioReportDetail,
    PortfolioReportRow,
)
from verticals.mga.portfolio_reporting.service import PortfolioService

router = APIRouter(prefix="/portfolio-reporting", tags=["mga:portfolio-reporting"])
_service = PortfolioService()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CtxDep = Annotated[Ctx, Depends(get_ctx)]


@router.get("", response_model=list[PortfolioReportRow])
async def list_reports(session: SessionDep, ctx: CtxDep) -> list[PortfolioReportRow]:
    return await _service.list_rows(session, ctx)


@router.post("/run", response_model=PortfolioReportDetail)
async def run_report(session: SessionDep, ctx: CtxDep, scenario: str) -> PortfolioReportDetail:
    """Dev/ingestion trigger: process one fixture scenario (a full-book period, a
    class/carrier loss-ratio segment, renewal outcomes, a bind-data completeness check,
    broker production figures, or an appetite exposure finding reference)."""
    try:
        return await _service.process(session, ctx, scenario)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/{submission_id}", response_model=PortfolioReportDetail)
async def get_report(session: SessionDep, ctx: CtxDep, submission_id: str) -> PortfolioReportDetail:
    detail = await _service.get_detail(session, ctx, submission_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "portfolio report not found")
    return detail


@router.post("/{submission_id}/act")
async def act_on_report(
    session: SessionDep, ctx: CtxDep, submission_id: str, body: ActRequest
) -> dict[str, str]:
    """Human action: approve (mark reviewed) | send (submit report externally) |
    escalate."""
    try:
        return await _service.act(session, ctx, submission_id, body.action)
    except AuthorityError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
