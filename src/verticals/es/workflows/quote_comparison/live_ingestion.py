"""Additive live-ingestion path for Quote Comparison: real carrier-response
emails from the tenant's connected Gmail, matched to a real submission by
its real named insured, instead of the static Workflow_13 scenario fixture
(``scenario_loader.py``, untouched by this module).

The actual parsing (``quote_parser.parse_response``) and comparison logic
(``comparison_engine.py``) are already real and fixture-agnostic — they read
plain ``From/Subject/Date`` + body text, with zero dependency on Workflow_13's
file naming. So the only new work here is getting a real Gmail message into
that exact text shape (``core.ingestion.connectors.LiveNangoConnectorService
.fetch_email_as_text``) and accumulating real responses per submission over
time (FR-3: "should update as each new response arrives, not require all
responses to be present").
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import Ctx, RawBundle, RawDocument
from core.common.enums import DocumentKind
from core.config import get_settings
from core.documents.store import LocalDocumentStore
from core.ingestion.connectors import (
    ConnectorNotConnectedError,
    LiveNangoConnectorService,
    build_connector_service,
)
from verticals.es.workflows.package_assembly.live_ingestion import build_live_extracted_model
from verticals.es.workflows.quote_comparison.quote_parser import ParsedResponse, parse_response

_DOC_KIND_PREFIXES = tuple(k.value for k in DocumentKind)
_RESPONSE_FILENAME_PREFIX = "response_"


async def _real_named_insured(session: AsyncSession, ctx: Ctx, submission_id: str) -> str | None:
    """Re-derives the real named insured from this submission's already-
    persisted documents — same mechanism already used by Package Assembly's
    and Agent Communication's live paths, reused (not duplicated in logic,
    just in this tiny lookup) a third time."""
    model = await build_live_extracted_model(session, ctx, submission_id)
    for prefix in _DOC_KIND_PREFIXES:
        match = next((f for f in model.fields if f.name == f"{prefix}.named_insured"), None)
        if match is not None and match.value not in (None, ""):
            return str(match.value)
    return None


def _require_live_connector(session: AsyncSession) -> LiveNangoConnectorService:
    connector = build_connector_service(session=session)
    if not isinstance(connector, LiveNangoConnectorService):
        raise ConnectorNotConnectedError(get_settings().nango_integration_mail)
    return connector


async def discover_live_carrier_responses(
    session: AsyncSession, ctx: Ctx, submission_id: str
) -> list[dict[str, Any]]:
    """Real Gmail messages that could be carrier responses for this real
    submission — searched by the submission's own real named insured (per
    your choice), so a broker only sees replies relevant to THIS placement,
    not the whole inbox. Returns an empty list (not an error) if no real
    named insured has been extracted yet — honestly nothing to search for."""
    named_insured = await _real_named_insured(session, ctx, submission_id)
    if not named_insured:
        return []
    connector = _require_live_connector(session)
    messages = await connector.fetch_inbox(ctx, since_cursor=f'subject:"{named_insured}"')
    return [{"id": m.id, "subject": m.subject} for m in messages]


async def save_live_response(
    session: AsyncSession, ctx: Ctx, submission_id: str, message_id: str
) -> None:
    """Fetches one real message as plain header+body text and persists it —
    a repeated call with the same message_id just overwrites the same real
    document (LocalDocumentStore keys by filename), so re-picking an email
    never duplicates it."""
    connector = _require_live_connector(session)
    text = await connector.fetch_email_as_text(ctx, message_id)
    await LocalDocumentStore().save(
        session, ctx, submission_id,
        RawDocument(
            kind=DocumentKind.EMAIL,
            filename=f"{_RESPONSE_FILENAME_PREFIX}{message_id}.txt",
            content=text,
        ),
    )


async def _load_response_documents(
    session: AsyncSession, ctx: Ctx, submission_id: str
) -> list[RawDocument]:
    docs = await LocalDocumentStore().list_for_submission(session, ctx, submission_id)
    return [d for d in docs if d.filename.startswith(_RESPONSE_FILENAME_PREFIX)]


async def load_live_responses(
    session: AsyncSession, ctx: Ctx, submission_id: str
) -> list[ParsedResponse]:
    """Every real carrier response persisted so far for this submission,
    parsed through the exact same quote_parser the fixture path uses — real
    accumulation across however many replies have arrived to date (FR-3)."""
    documents = await _load_response_documents(session, ctx, submission_id)
    return [parse_response(d.filename, d.content or "") for d in documents]


async def load_live_response_bundle(
    session: AsyncSession, ctx: Ctx, submission_id: str
) -> RawBundle:
    """Same accumulated real responses as ``load_live_responses``, but as a
    ``RawBundle`` of unparsed ``RawDocument``s — the exact shape
    ``QuoteComparisonPipeline.ingest_live()`` hands to the pipeline's own
    (unchanged) ``extract()`` stage, which parses them itself just like the
    fixture path does."""
    documents = await _load_response_documents(session, ctx, submission_id)
    return RawBundle(submission_id=submission_id, documents=documents)
