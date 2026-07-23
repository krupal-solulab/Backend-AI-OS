"""Jobs (Phase 1) — async ingestion→extraction on Arq/Redis, with a queryable error
queue.

The ``ingest_and_extract`` task pulls a (mock) submission via the ConnectorService and
runs extraction, tracking every run in the ``job_run`` table. Failed runs are marked
``error`` and remain queryable via ``JobRunService.errors`` — a visible error queue that
works even without Redis (the DB is the record of truth). ``WorkerSettings`` wires the
real Arq worker for production. See ``core.jobs.worker``.
"""

from core.jobs.service import (
    JobRunService,
    JobStatus,
    ingest_and_extract,
)
from core.jobs.worker import WorkerSettings

__all__ = [
    "JobRunService",
    "JobStatus",
    "WorkerSettings",
    "ingest_and_extract",
]
