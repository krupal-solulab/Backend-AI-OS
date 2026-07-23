"""Alembic environment — async engine, DB URL + metadata sourced from the app.

Registers every table on ``SQLModel.metadata`` so autogenerate/``check`` see the full
target schema: the shared base (``core.models``) plus each vertical's tables, which are
auto-discovered by globbing ``verticals/*/models.py``. This means a new vertical (or a
new dev) never edits this file — dropping a ``verticals/<vertical>/models.py`` is enough.
Uses the same DATABASE_URL as the app, so the target is SQLite in dev and Postgres later.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import pkgutil
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers shared base tables on SQLModel.metadata)
import verticals  # the verticals namespace, scanned below
from core.config import get_settings


def _register_vertical_models() -> list[str]:
    """Import every ``verticals/<vertical>/models.py`` so its tables register on
    ``SQLModel.metadata``. A vertical without a ``models.py`` is skipped; a ``models.py``
    that fails to import raises loudly (we never silently swallow a broken module)."""
    loaded: list[str] = []
    for mod in pkgutil.iter_modules(verticals.__path__, prefix="verticals."):
        if not mod.ispkg:
            continue
        models_module = f"{mod.name}.models"
        if importlib.util.find_spec(models_module) is None:
            continue  # this vertical simply has no tables yet
        importlib.import_module(models_module)  # import errors propagate, by design
        loaded.append(models_module)
    return loaded


_register_vertical_models()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # batch mode → portable ALTERs (SQLite-friendly)
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
