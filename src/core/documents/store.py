"""Local-disk document store with DB metadata."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx, RawDocument
from core.config import Settings, get_settings
from core.models import Document

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(part: str) -> str:
    return _SAFE.sub("_", part) or "_"


class DocumentStore(Protocol):
    async def save(self, session: AsyncSession, ctx: Ctx, submission_id: str,
                   doc: RawDocument) -> str: ...
    async def get(self, session: AsyncSession, ctx: Ctx, document_id: str) -> RawDocument: ...
    async def list_for_submission(self, session: AsyncSession, ctx: Ctx,
                                  submission_id: str) -> list[RawDocument]: ...


class LocalDocumentStore:
    """Bytes on disk under ``DOCUMENT_STORAGE_ROOT/<tenant>/<submission>/``; metadata in DB."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._root = Path((settings or get_settings()).document_storage_root)

    def _path(self, tenant_id: str, submission_id: str, filename: str) -> Path:
        return self._root / _safe(tenant_id) / _safe(submission_id) / _safe(filename)

    async def save(
        self, session: AsyncSession, ctx: Ctx, submission_id: str, doc: RawDocument
    ) -> str:
        path = self._path(ctx.tenant_id, submission_id, doc.filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc.content or "", encoding="utf-8")

        row = Document(
            tenant_id=ctx.tenant_id,
            submission_id=submission_id,
            kind=doc.kind,
            filename=doc.filename,
            uri=str(path),
            content=doc.content,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id

    async def get(self, session: AsyncSession, ctx: Ctx, document_id: str) -> RawDocument:
        row = (
            await session.execute(
                select(Document).where(
                    col(Document.id) == document_id, col(Document.tenant_id) == ctx.tenant_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise KeyError(f"document '{document_id}' not found for tenant")
        content = row.content
        if content is None and row.uri and Path(row.uri).is_file():
            content = Path(row.uri).read_text(encoding="utf-8")
        return RawDocument(kind=row.kind, filename=row.filename, content=content or "", uri=row.uri)

    async def list_for_submission(
        self, session: AsyncSession, ctx: Ctx, submission_id: str
    ) -> list[RawDocument]:
        rows = (
            await session.execute(
                select(Document)
                .where(
                    col(Document.tenant_id) == ctx.tenant_id,
                    col(Document.submission_id) == submission_id,
                )
                .order_by(col(Document.filename))
            )
        ).scalars().all()
        return [
            RawDocument(kind=r.kind, filename=r.filename, content=r.content or "", uri=r.uri)
            for r in rows
        ]
