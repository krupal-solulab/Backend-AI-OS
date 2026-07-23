"""Arq worker settings for production.

Run with:  arq core.jobs.worker.WorkerSettings   (requires a running Redis at REDIS_URL)
"""

from __future__ import annotations

from arq.connections import RedisSettings

from core.config import get_settings
from core.jobs.service import ingest_and_extract


class WorkerSettings:
    """Arq entrypoint. ``functions`` are the tasks the worker can run."""

    functions = [ingest_and_extract]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
