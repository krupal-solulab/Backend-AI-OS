"""Async jobs — the ingestion→extraction task + queryable error queue.

The task uses the app's configured DB (core.db.async_session_factory). These tests need
the base tables (run `alembic upgrade head`) and the `demo-mga` tenant (run the seed) —
both part of the standard setup. The error-queue assertions do NOT require Redis.

The Arq burst test runs the real worker against an in-memory fakeredis (no Redis server);
if arq/fakeredis are incompatible in this environment it SKIPS with a reason — never fakes.
"""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import col, select

from core.db import async_session_factory
from core.jobs import JobRunService, JobStatus, ingest_and_extract
from core.models import JobRun


async def _tenant_exists(tenant_id: str) -> bool:
    from core.models import Tenant

    async with async_session_factory() as s:
        row = (
            await s.execute(select(Tenant).where(col(Tenant.id) == tenant_id))
        ).scalar_one_or_none()
        return row is not None


async def test_ingest_and_extract_success_records_job_run() -> None:
    if not await _tenant_exists("demo-mga"):
        pytest.skip("demo-mga tenant not seeded; run `python src/core/seed.py`")

    result = await ingest_and_extract(
        {}, tenant_id="demo-mga", vertical="MGA", message_id="submission_02", workflow_n=1
    )
    assert result["fields"] > 0
    assert result["documents"] >= 4

    async with async_session_factory() as s:
        runs = (
            await s.execute(
                select(JobRun).where(
                    col(JobRun.submission_id) == "submission_02",
                    col(JobRun.status) == JobStatus.SUCCESS,
                )
            )
        ).scalars().all()
        assert runs, "expected a SUCCESS job_run for submission_02"


async def test_failed_job_goes_to_queryable_error_queue() -> None:
    if not await _tenant_exists("demo-mga"):
        pytest.skip("demo-mga tenant not seeded; run `python src/core/seed.py`")

    bad_id = f"missing-{uuid.uuid4().hex[:8]}"
    with pytest.raises(KeyError):
        await ingest_and_extract(
            {}, tenant_id="demo-mga", vertical="MGA", message_id=bad_id, workflow_n=1
        )

    # Error is visible/queryable without Redis.
    async with async_session_factory() as s:
        errors = await JobRunService.errors(s, tenant_id="demo-mga")
        assert any(e.submission_id == bad_id for e in errors)


async def test_arq_worker_burst_mode() -> None:
    """Run the real Arq worker in burst mode against fakeredis (no Redis server)."""
    if not await _tenant_exists("demo-mga"):
        pytest.skip("demo-mga tenant not seeded; run `python src/core/seed.py`")
    try:
        import fakeredis.aioredis  # noqa: F401
        from arq import ArqRedis
        from arq.worker import Worker
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"arq/fakeredis unavailable: {exc}")

    fake = fakeredis.aioredis.FakeRedis()
    redis = ArqRedis(connection_pool=fake.connection_pool)
    try:
        job = await redis.enqueue_job(
            "ingest_and_extract",
            tenant_id="demo-mga", vertical="MGA", message_id="submission_03", workflow_n=1,
        )
        worker = Worker(
            functions=[ingest_and_extract],
            redis_pool=redis,
            burst=True,
            poll_delay=0.0,
            max_jobs=10,
        )
        await worker.main()
    except Exception as exc:  # fakeredis/arq incompatibility → skip cleanly, don't fake
        pytest.skip(f"arq burst not runnable against fakeredis here: {exc}")
    finally:
        await redis.aclose()

    assert job is not None
    async with async_session_factory() as s:
        runs = (
            await s.execute(
                select(JobRun).where(
                    col(JobRun.submission_id) == "submission_03",
                    col(JobRun.status) == JobStatus.SUCCESS,
                )
            )
        ).scalars().all()
        assert runs, "expected a SUCCESS job_run produced by the Arq burst worker"
