"""Proves LiveNangoConnectorService against a mocked Nango proxy (httpx.MockTransport
— no extra test dependency, no real network) — the real Gmail connector added for
the Nango live-connector phase (docs/CONNECTORS_NANGO.md). Mock-mode connector
behavior (MockConnectorService) is completely untouched by this file.
"""

from __future__ import annotations

import base64
import io

import httpx
import pytest
from reportlab.pdfgen import canvas
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.common.dtos import Ctx
from core.common.enums import DocumentKind, Role, Vertical
from core.config import get_settings
from core.extraction import DefaultExtractionService
from core.ingestion.connectors import ConnectorNotConnectedError, LiveNangoConnectorService
from core.integrations.repository import upsert_connection
from core.models import Tenant


@pytest.fixture
async def es_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(Tenant(id="demo-es", name="Demo E&S", vertical=Vertical.ES))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def es_ctx() -> Ctx:
    return Ctx(tenant_id="demo-es", vertical=Vertical.ES, user_id="u-jr", role=Role.JUNIOR)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


# Invalid UTF-8 (0xFF is never a valid leading byte) — stands in for a real binary
# PDF attachment, to prove decoding never silently corrupts/guesses text.
_FAKE_PDF_BYTES = b"\xff\xfe\xfd\xfc-not-real-utf8"


def _build_real_acord_pdf() -> bytes:
    """A genuinely real, fillable PDF (built with reportlab) — proves the whole
    live-connector -> extraction path against an actual document, not a mock."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(400, 300))
    c.drawString(20, 260, "ACORD 125 - COMMERCIAL INSURANCE APPLICATION")
    form = c.acroForm
    form.textfield(
        name="named_insured", tooltip="Named Insured",
        x=20, y=200, width=250, height=20,
        value="Delta Electric Services LLC", forceBorder=True,
    )
    c.save()
    return buf.getvalue()


_REAL_ACORD_PDF_BYTES = _build_real_acord_pdf()


def _gmail_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/proxy/gmail/v1/users/me/messages" and request.method == "GET":
        return httpx.Response(200, json={"messages": [{"id": "msg-1"}]})
    if path == "/proxy/gmail/v1/users/me/messages/msg-1" and request.method == "GET":
        if request.url.params.get("format") == "metadata":
            return httpx.Response(
                200, json={"payload": {"headers": [{"name": "Subject", "value": "Test Subject"}]}}
            )
        return httpx.Response(
            200,
            json={
                "id": "msg-1",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _b64url(b"Hello broker, please quote this.")},
                        },
                        {
                            "filename": "acord_application.pdf",
                            "mimeType": "application/pdf",
                            "body": {"attachmentId": "att-1"},
                        },
                    ],
                },
            },
        )
    if path == "/proxy/gmail/v1/users/me/messages/msg-1/attachments/att-1":
        return httpx.Response(200, json={"data": _b64url(_FAKE_PDF_BYTES)})
    if path == "/proxy/gmail/v1/users/me/messages/msg-2" and request.method == "GET":
        return httpx.Response(
            200,
            json={
                "id": "msg-2",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _b64url(b"New submission, ACORD attached.")},
                        },
                        {
                            "filename": "acord_application.pdf",
                            "mimeType": "application/pdf",
                            "body": {"attachmentId": "att-2"},
                        },
                    ],
                },
            },
        )
    if path == "/proxy/gmail/v1/users/me/messages/msg-2/attachments/att-2":
        return httpx.Response(200, json={"data": _b64url(_REAL_ACORD_PDF_BYTES)})
    if path == "/proxy/gmail/v1/users/me/messages/send" and request.method == "POST":
        return httpx.Response(200, json={"id": "sent-1", "threadId": "thread-1"})
    return httpx.Response(404, json={"error": f"unhandled {request.method} {path}"})


@pytest.fixture
def mocked_nango_transport(monkeypatch):
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_gmail_handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def _connected_service(es_ctx, es_session) -> LiveNangoConnectorService:
    await upsert_connection(
        es_session, es_ctx.tenant_id, "google-mail",
        nango_connection_id="conn-1", status="connected",
    )
    return LiveNangoConnectorService(get_settings(), es_session)


async def test_connector_not_connected_raises_without_connecting(es_ctx, es_session) -> None:
    """No Connection row for this tenant/provider — must raise a clear domain error,
    never silently return an empty/fabricated result."""
    service = LiveNangoConnectorService(get_settings(), es_session)
    with pytest.raises(ConnectorNotConnectedError):
        await service.fetch_inbox(es_ctx)


async def test_fetch_inbox_lists_real_messages_lightweight(
    es_ctx, es_session, mocked_nango_transport
) -> None:
    """Inbox list is metadata-only (subject) — body/attachments stay blank here and
    are fetched later via to_raw_bundle for the one message actually picked, not
    hydrated for every message in a real inbox."""
    service = await _connected_service(es_ctx, es_session)
    messages = await service.fetch_inbox(es_ctx)
    assert len(messages) == 1
    assert messages[0].id == "msg-1"
    assert messages[0].subject == "Test Subject"
    assert messages[0].body == ""


async def test_to_raw_bundle_extracts_real_body_and_attachment(
    es_ctx, es_session, mocked_nango_transport
) -> None:
    service = await _connected_service(es_ctx, es_session)
    bundle = await service.to_raw_bundle(es_ctx, "msg-1")
    assert bundle.submission_id == "msg-1"
    assert "Hello broker" in (bundle.email_body or "")
    assert len(bundle.documents) == 1
    assert bundle.documents[0].filename == "acord_application.pdf"
    assert bundle.documents[0].kind == DocumentKind.ACORD


async def test_get_attachments_decodes_binary_content_without_corrupting(
    es_ctx, es_session, mocked_nango_transport
) -> None:
    """The fake attachment bytes aren't valid utf-8 text — must come back as a
    base64 string (honest, inspectable), never a guessed/garbled decode."""
    service = await _connected_service(es_ctx, es_session)
    blobs = await service.get_attachments(es_ctx, "msg-1")
    assert len(blobs) == 1
    assert blobs[0].content == base64.b64encode(_FAKE_PDF_BYTES).decode("ascii")


async def test_send_email_posts_real_raw_message(
    es_ctx, es_session, mocked_nango_transport
) -> None:
    service = await _connected_service(es_ctx, es_session)
    ref = await service.send_email(
        es_ctx, {"to": "broker@example.com", "subject": "Re: Submission", "body": "Thanks!"}
    )
    assert ref.to == "broker@example.com"
    assert ref.thread_id == "thread-1"


async def test_real_pdf_attachment_extracted_and_field_reaches_extraction(
    es_ctx, es_session, mocked_nango_transport
) -> None:
    """End-to-end proof of the Part 4 fix: a genuinely real (reportlab-built,
    fillable) PDF attachment flows through the live connector's to_raw_bundle and
    then through the REAL DefaultExtractionService — the actual
    ``acord.named_insured`` field must come out correctly classified and cited,
    not left as an opaque base64 blob."""
    service = await _connected_service(es_ctx, es_session)
    bundle = await service.to_raw_bundle(es_ctx, "msg-2")

    assert len(bundle.documents) == 1
    assert "Named Insured: Delta Electric Services LLC" in bundle.documents[0].content

    model = await DefaultExtractionService().extract(es_ctx, bundle)
    named_insured = next((f for f in model.fields if f.name == "acord.named_insured"), None)
    assert named_insured is not None
    assert named_insured.value == "Delta Electric Services LLC"
    assert named_insured.confidence == 1.0
    assert named_insured.citation is not None
    assert named_insured.citation.filename == "acord_application.pdf"


async def test_fetch_inbox_uses_configured_default_query(es_ctx, es_session, monkeypatch) -> None:
    """Proves the Part 4 inbox-filter fix: fetch_inbox sends Settings'
    nango_inbox_query (subject:submission by default) rather than the old
    unfiltered 'in:inbox newer_than:30d'."""
    captured_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/gmail/v1/users/me/messages":
            captured_queries.append(request.url.params.get("q", ""))
            return httpx.Response(200, json={"messages": []})
        return httpx.Response(404, json={"error": "unexpected request"})

    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    service = await _connected_service(es_ctx, es_session)
    await service.fetch_inbox(es_ctx)

    assert captured_queries == [get_settings().nango_inbox_query]
    assert "subject:submission" in captured_queries[0]
