"""Connector interface + mock (fixtures) and live-REST-stub implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from core.common.dtos import Ctx, RawBundle, RawDocument
from core.common.enums import DocumentKind
from core.config import Settings, get_settings


@dataclass
class EmailMessage:
    id: str
    submission_ref: str
    subject: str
    body: str
    attachments: list[str] = field(default_factory=list)


@dataclass
class FileBlob:
    filename: str
    content: str
    kind: DocumentKind = DocumentKind.OTHER


@dataclass
class SentRef:
    to: str
    subject: str
    thread_id: str | None = None


class ConnectorService(Protocol):
    async def fetch_inbox(
        self, ctx: Ctx, since_cursor: str | None = None
    ) -> list[EmailMessage]: ...
    async def get_attachments(self, ctx: Ctx, message_id: str) -> list[FileBlob]: ...
    async def send_email(self, ctx: Ctx, message: dict[str, Any]) -> SentRef: ...
    async def to_raw_bundle(self, ctx: Ctx, message_id: str) -> RawBundle: ...


class MockConnectorService:
    """Serves emails + attachments from the offline ``Workflow_<n>`` fixtures. Enables
    building the whole pipeline without a live mailbox."""

    def __init__(self, workflow_n: int = 1) -> None:
        self._workflow_n = workflow_n
        self._sent: list[SentRef] = []

    def _load(self, ctx: Ctx) -> dict[str, Any]:
        from fixtures import load_workflow  # local import avoids cycle at module load

        loaded = load_workflow(
            self._workflow_n, tenant_id=ctx.tenant_id, vertical=ctx.vertical
        )
        return {ls.submission.external_ref or ls.submission.id: ls for ls in loaded}

    async def fetch_inbox(
        self, ctx: Ctx, since_cursor: str | None = None
    ) -> list[EmailMessage]:
        messages: list[EmailMessage] = []
        for ref, ls in self._load(ctx).items():
            email_doc = next((d for d in ls.documents if d.kind is DocumentKind.EMAIL), None)
            attachments = [d.filename for d in ls.documents if d.kind is not DocumentKind.EMAIL]
            messages.append(
                EmailMessage(
                    id=ref,
                    submission_ref=ref,
                    subject=ls.submission.subject or ref,
                    body=(email_doc.content if email_doc else "") or "",
                    attachments=attachments,
                )
            )
        return messages

    async def get_attachments(self, ctx: Ctx, message_id: str) -> list[FileBlob]:
        ls = self._load(ctx).get(message_id)
        if ls is None:
            return []
        return [
            FileBlob(filename=d.filename, content=d.content or "", kind=d.kind)
            for d in ls.documents
            if d.kind is not DocumentKind.EMAIL
        ]

    async def to_raw_bundle(self, ctx: Ctx, message_id: str) -> RawBundle:
        ls = self._load(ctx).get(message_id)
        if ls is None:
            raise KeyError(f"no fixture submission '{message_id}' for Workflow_{self._workflow_n}")
        email_doc = next((d for d in ls.documents if d.kind is DocumentKind.EMAIL), None)
        documents = [
            RawDocument(kind=d.kind, filename=d.filename, content=d.content or "", uri=d.uri)
            for d in ls.documents
        ]
        return RawBundle(
            submission_id=message_id,
            email_body=(email_doc.content if email_doc else None),
            documents=documents,
        )

    async def send_email(self, ctx: Ctx, message: dict[str, Any]) -> SentRef:
        """Records a send (offline). MUST only be called from a human action — never
        from the ingestion path (no auto-send)."""
        ref = SentRef(
            to=message.get("to", ""),
            subject=message.get("subject", ""),
            thread_id=message.get("thread_id"),
        )
        self._sent.append(ref)
        return ref


class LiveNangoConnectorService:
    """Live path skeleton. Wires to the Nango REST proxy, but makes NO network calls in
    Phase 1 — every method raises until Phase 1.6 enables real Nango. This keeps the
    interface/factory complete without any live traffic or auto-send risk."""

    _PHASE = "live Nango connectors are enabled in Phase 1.6 (CONNECTORS_NANGO.md); not active"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings  # NANGO_HOST / NANGO_SECRET_KEY / integrations

    async def fetch_inbox(
        self, ctx: Ctx, since_cursor: str | None = None
    ) -> list[EmailMessage]:
        raise NotImplementedError(self._PHASE)

    async def get_attachments(self, ctx: Ctx, message_id: str) -> list[FileBlob]:
        raise NotImplementedError(self._PHASE)

    async def send_email(self, ctx: Ctx, message: dict[str, Any]) -> SentRef:
        raise NotImplementedError(self._PHASE)

    async def to_raw_bundle(self, ctx: Ctx, message_id: str) -> RawBundle:
        raise NotImplementedError(self._PHASE)


def build_connector_service(
    settings: Settings | None = None, *, workflow_n: int = 1
) -> ConnectorService:
    """Factory: ``CONNECTORS_MODE=mock`` → fixtures; ``live`` → Nango REST skeleton."""
    settings = settings or get_settings()
    if settings.connectors_mode == "live":
        return LiveNangoConnectorService(settings)
    return MockConnectorService(workflow_n=workflow_n)
