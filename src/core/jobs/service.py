"""Job-run tracking + the ingestion→extraction task."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.common.enums import Role, Vertical
from core.db import async_session_factory
from core.extraction import DefaultExtractionService
from core.ingestion import build_connector_service
from core.models import JobRun


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


class JobRunService:
    """CRUD around the ``job_run`` table — the queryable job log + error queue."""

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        job_name: str,
        tenant_id: str | None = None,
        submission_id: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> str:
        row = JobRun(
            job_name=job_name,
            tenant_id=tenant_id,
            submission_id=submission_id,
            args=args,
            status=JobStatus.QUEUED,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id

    @staticmethod
    async def mark(
        session: AsyncSession,
        run_id: str,
        status: JobStatus,
        *,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        row = (
            await session.execute(select(JobRun).where(col(JobRun.id) == run_id))
        ).scalar_one_or_none()
        if row is None:
            return
        row.status = status
        row.updated_at = datetime.now(UTC)
        if status is JobStatus.RUNNING:
            row.attempts += 1
        if error is not None:
            row.error = error
        if result is not None:
            row.result = result
        session.add(row)
        await session.commit()

    @staticmethod
    async def errors(
        session: AsyncSession, tenant_id: str | None = None
    ) -> list[JobRun]:
        """The error queue: all runs that ended in ``error`` (optionally tenant-scoped)."""
        stmt = select(JobRun).where(col(JobRun.status) == JobStatus.ERROR)
        if tenant_id is not None:
            stmt = stmt.where(col(JobRun.tenant_id) == tenant_id)
        stmt = stmt.order_by(col(JobRun.updated_at))
        return list((await session.execute(stmt)).scalars().all())


async def ingest_and_extract(
    arq_ctx: dict[str, Any],
    *,
    tenant_id: str,
    vertical: str,
    message_id: str,
    workflow_n: int = 1,
) -> dict[str, Any]:
    """Arq task: (mock) ingest a submission → extract → record the run.

    Tracks the run in ``job_run``; on failure marks it ``error`` (queryable) and
    re-raises so Arq's own retry/failure handling also sees it. Runs identically whether
    driven by the Arq worker or called directly (used by the no-Redis test).
    """
    ctx = Ctx(
        tenant_id=tenant_id,
        vertical=Vertical(vertical),
        user_id="system",
        role=Role.ADMIN,
    )
    async with async_session_factory() as session:
        run_id = await JobRunService.create(
            session,
            job_name="ingest_and_extract",
            tenant_id=tenant_id,
            submission_id=message_id,
            args={"message_id": message_id, "workflow_n": workflow_n},
        )
        await JobRunService.mark(session, run_id, JobStatus.RUNNING)
        try:
            connector = build_connector_service(workflow_n=workflow_n)
            raw = await connector.to_raw_bundle(ctx, message_id)
            model = await DefaultExtractionService().extract(ctx, raw)
            result = {
                "submission_id": message_id,
                "documents": len(raw.documents),
                "fields": len(model.fields),
            }
            await JobRunService.mark(session, run_id, JobStatus.SUCCESS, result=result)
            return result
        except Exception as exc:
            await JobRunService.mark(session, run_id, JobStatus.ERROR, error=str(exc))
            raise
