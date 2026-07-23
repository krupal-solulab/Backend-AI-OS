"""Async database engine + session factory.

Dev default is SQLite via aiosqlite; the same models are Postgres-compatible (no
PG-native types), so DATABASE_URL can flip to ``postgresql+asyncpg://...`` with no
code change. Alembic owns schema creation — we never call ``create_all`` in the app.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    echo=False,
    future=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a tenant-agnostic async session."""
    async with async_session_factory() as session:
        yield session
