"""Connector interface + mock (fixtures) and live (Nango REST proxy) implementations."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeEmailMessage
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import Ctx, RawBundle, RawDocument
from core.common.enums import DocumentKind
from core.config import Settings, get_settings
from core.ingestion.document_text import extract_text
from fixtures.loader import _classify  # reuse the same filename -> DocumentKind inference


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


class ConnectorNotConnectedError(Exception):
    """Raised by the live connector when a tenant has no active Nango connection for a
    provider yet — the caller (a router) should translate this into a 428 asking the
    user to connect the integration first, not a 500."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(f"no active connection for provider '{provider}'")


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


def _b64url_to_bytes(data: str) -> bytes:
    """Gmail's base64url, un-padded -> raw bytes."""
    if not data:
        return b""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _decode_gmail_base64(data: str) -> str:
    """Text-only resolver, used for the email body (always plain text/HTML from
    Gmail, never a file attachment). Binary content is re-encoded as plain base64
    text rather than corrupted or guessed."""
    raw = _b64url_to_bytes(data)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(raw).decode("ascii")


def _resolve_attachment_content(filename: str, raw: bytes) -> str:
    """Real PDF/DOCX/XLSX text/form-field extraction first (document_text.py);
    falls back to the same utf-8-or-base64 behavior as the email body when
    extraction finds nothing (unsupported format, corrupt file, scanned/image-only
    PDF — never fabricated, never crashes on a bad real-world attachment)."""
    extracted = extract_text(filename, raw)
    if extracted is not None:
        return extracted
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(raw).decode("ascii")


def _find_mime_part(payload: dict[str, Any], mime_type: str) -> dict[str, Any] | None:
    if payload.get("mimeType") == mime_type:
        return payload
    for part in payload.get("parts") or []:
        found = _find_mime_part(part, mime_type)
        if found is not None:
            return found
    return None


def _iter_leaf_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts = payload.get("parts")
    if not parts:
        return [payload]
    leaves: list[dict[str, Any]] = []
    for part in parts:
        leaves.extend(_iter_leaf_parts(part))
    return leaves


def _extract_body(payload: dict[str, Any]) -> str:
    plain = _find_mime_part(payload, "text/plain")
    if plain is not None:
        return _decode_gmail_base64(plain.get("body", {}).get("data", ""))
    html = _find_mime_part(payload, "text/html")
    if html is not None:
        raw_html = _decode_gmail_base64(html.get("body", {}).get("data", ""))
        return re.sub(r"<[^>]+>", "", raw_html)
    return _decode_gmail_base64(payload.get("body", {}).get("data", ""))


class LiveNangoConnectorService:
    """Live path: proxies Gmail calls through Nango for the tenant's connected mailbox
    (docs/CONNECTORS_NANGO.md). Every call resolves the tenant's ``Connection`` row
    (google-mail) first and raises ``ConnectorNotConnectedError`` if none exists."""

    def __init__(self, settings: Settings, session: AsyncSession | None = None) -> None:
        self._settings = settings  # NANGO_HOST / NANGO_SECRET_KEY / integrations
        self._session = session
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._settings.nango_host,
                headers={"Authorization": f"Bearer {self._settings.nango_secret_key}"},
                timeout=30.0,
            )
        return self._client

    async def _proxy_headers(self, ctx: Ctx) -> dict[str, str]:
        provider = self._settings.nango_integration_mail
        if self._session is None:
            raise ConnectorNotConnectedError(provider)
        from core.integrations.repository import get_connection  # avoids import-time cycle

        conn = await get_connection(self._session, ctx.tenant_id, provider)
        if conn is None or conn.status != "connected" or not conn.nango_connection_id:
            raise ConnectorNotConnectedError(provider)
        return {"Connection-Id": conn.nango_connection_id, "Provider-Config-Key": provider}

    async def fetch_inbox(
        self, ctx: Ctx, since_cursor: str | None = None
    ) -> list[EmailMessage]:
        client = self._get_client()
        headers = await self._proxy_headers(ctx)
        query = since_cursor or self._settings.nango_inbox_query
        resp = await client.get(
            "/proxy/gmail/v1/users/me/messages", headers=headers, params={"q": query}
        )
        resp.raise_for_status()
        ids = [m["id"] for m in resp.json().get("messages", [])]

        # Metadata-only per message — real inboxes can be large, so body/attachments are
        # left blank here and fetched later (via to_raw_bundle) only for the one message
        # actually chosen, not for every message in the picker list.
        messages: list[EmailMessage] = []
        for message_id in ids:
            meta = await client.get(
                f"/proxy/gmail/v1/users/me/messages/{message_id}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": ["Subject", "From"]},
            )
            meta.raise_for_status()
            msg_headers = meta.json().get("payload", {}).get("headers", [])
            subject = next(
                (h["value"] for h in msg_headers if h["name"] == "Subject"), message_id
            )
            messages.append(
                EmailMessage(
                    id=message_id, submission_ref=message_id, subject=subject, body="",
                )
            )
        return messages

    async def get_attachments(self, ctx: Ctx, message_id: str) -> list[FileBlob]:
        client = self._get_client()
        headers = await self._proxy_headers(ctx)
        resp = await client.get(
            f"/proxy/gmail/v1/users/me/messages/{message_id}",
            headers=headers,
            params={"format": "full"},
        )
        resp.raise_for_status()
        payload = resp.json().get("payload", {})

        blobs: list[FileBlob] = []
        for part in _iter_leaf_parts(payload):
            filename = part.get("filename") or ""
            attachment_id = part.get("body", {}).get("attachmentId")
            if not filename or not attachment_id:
                continue
            att = await client.get(
                f"/proxy/gmail/v1/users/me/messages/{message_id}/attachments/{attachment_id}",
                headers=headers,
            )
            att.raise_for_status()
            raw = _b64url_to_bytes(att.json().get("data", ""))
            content = _resolve_attachment_content(filename, raw)
            blobs.append(FileBlob(filename=filename, content=content, kind=_classify(filename)))
        return blobs

    async def to_raw_bundle(self, ctx: Ctx, message_id: str) -> RawBundle:
        client = self._get_client()
        headers = await self._proxy_headers(ctx)
        resp = await client.get(
            f"/proxy/gmail/v1/users/me/messages/{message_id}",
            headers=headers,
            params={"format": "full"},
        )
        resp.raise_for_status()
        payload = resp.json().get("payload", {})

        body = _extract_body(payload)
        attachments = await self.get_attachments(ctx, message_id)
        documents = [
            RawDocument(kind=a.kind, filename=a.filename, content=a.content) for a in attachments
        ]
        return RawBundle(submission_id=message_id, email_body=body, documents=documents)

    async def fetch_email_as_text(self, ctx: Ctx, message_id: str) -> str:
        """Real message, reconstructed as plain ``From/Subject/Date + body`` text
        — the exact shape ``quote_comparison/quote_parser.py`` already expects
        (built to parse this from the Workflow_13 fixture files, with no
        fixture-specific dependency beyond that shape). Deliberately NOT Gmail's
        raw RFC822 export (``format=raw``) — that carries MIME boundary/
        Content-Type noise the parser's header/body split was never built to
        ignore; reusing the same clean header-list + ``_extract_body`` split
        already used elsewhere in this class avoids that."""
        client = self._get_client()
        headers = await self._proxy_headers(ctx)
        resp = await client.get(
            f"/proxy/gmail/v1/users/me/messages/{message_id}",
            headers=headers,
            params={"format": "full"},
        )
        resp.raise_for_status()
        payload = resp.json().get("payload", {})
        msg_headers = payload.get("headers", [])

        def _header(name: str) -> str:
            return next(
                (h["value"] for h in msg_headers if h["name"].lower() == name.lower()), ""
            )

        body = _extract_body(payload)
        return (
            f"From: {_header('From')}\n"
            f"Subject: {_header('Subject')}\n"
            f"Date: {_header('Date')}\n\n"
            f"{body}"
        )

    async def send_email(self, ctx: Ctx, message: dict[str, Any]) -> SentRef:
        """Sends a real email via Gmail (Nango proxy). MUST only be called from a
        human-triggered action — never from the ingestion path (no auto-send)."""
        client = self._get_client()
        headers = await self._proxy_headers(ctx)

        mime = MimeEmailMessage()
        mime["To"] = message.get("to", "")
        mime["Subject"] = message.get("subject", "")
        thread_id = message.get("thread_id")
        if thread_id:
            mime["In-Reply-To"] = thread_id
        mime.set_content(message.get("body", ""))
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")

        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        resp = await client.post(
            "/proxy/gmail/v1/users/me/messages/send", headers=headers, json=body
        )
        resp.raise_for_status()
        sent = resp.json()
        return SentRef(
            to=message.get("to", ""),
            subject=message.get("subject", ""),
            thread_id=sent.get("threadId"),
        )


def build_connector_service(
    settings: Settings | None = None,
    *,
    workflow_n: int = 1,
    session: AsyncSession | None = None,
) -> ConnectorService:
    """Factory: ``CONNECTORS_MODE=mock`` -> fixtures; ``live`` -> real Nango proxy.

    ``session`` is only needed by the live path (to resolve the tenant's Connection
    row) — it's optional and ignored by MockConnectorService, so every existing
    fixture-mode call site keeps working unchanged.
    """
    settings = settings or get_settings()
    if settings.connectors_mode == "live":
        return LiveNangoConnectorService(settings, session)
    return MockConnectorService(workflow_n=workflow_n)
