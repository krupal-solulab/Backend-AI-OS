"""Additive real-ingestion path for Binder & Policy Issuance: builds a
``broker_bind_instruction``-shaped dict from an ACTUAL, already-selected
Quote Comparison record, instead of the static Workflow_14 fixture
(``scenario_loader.py``, untouched by this module) — the genuine
Quote Comparison -> Binder & Policy Issuance hand-off this vertical always
implied but never wired: a broker selecting a quote (``/quote-comparison/
{item_id}/select/{quote_id}``) is a real decision that should be able to
start a real bind order, not just fire an Agent Communication draft.

Honest limitations, by design, not oversight:
- Quote Comparison's real payload never sets ``carrier_id`` on a quote (only
  ``carrier_name``), so ``carrier_id`` here is resolved via the same real
  Carrier Appetite Profile panel lookup already reused throughout this
  vertical (``decision_core.carrier_profiles.load_carrier_panel``) — if no
  carrier in that panel matches the name, ``carrier_id`` stays ``None``
  honestly rather than being invented.
- Every subjectivity carried over from a just-selected quote is, by
  construction, still open and still pre-bind — Quote Comparison's
  ``SubjectivityOut`` has no clearance-tracking or lifecycle field at all,
  so ``status="open"`` and an absent ``lifecycle_stage`` (which
  ``classify_subjectivities`` already defaults to ``PRE_BIND``) are the only
  values that could ever be true here, not fabricated defaults.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx, RawDocument
from core.common.enums import DocumentKind
from core.config import get_settings
from core.documents.store import LocalDocumentStore
from core.ingestion.connectors import (
    ConnectorNotConnectedError,
    LiveNangoConnectorService,
    build_connector_service,
)
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from verticals.es.decision_core.carrier_profiles import load_carrier_panel

_CARRIER_PANEL_WORKFLOW_N = 10  # same real panel reused throughout this vertical
_CONFIRMATION_FILENAME = "carrier_bind_confirmation.txt"
_ISSUED_POLICY_FILENAME = "issued_policy_document_extract.txt"


async def build_broker_bind_instruction_from_quote(
    session: AsyncSession, ctx: Ctx, quote_comparison_item_id: str
) -> dict[str, Any]:
    """The live-data equivalent of a ``broker_bind_instruction.json``
    fixture, built from a real, already-selected Quote Comparison item."""
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == quote_comparison_item_id,
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.workflow) == "quote_comparison",
            )
        )
    ).scalar_one_or_none()
    if item is None or not item.output_package_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no quote-comparison review item '{quote_comparison_item_id}'",
        )
    pkg = (
        await session.execute(
            select(OutputPackageRow).where(col(OutputPackageRow.id) == item.output_package_id)
        )
    ).scalar_one_or_none()
    if pkg is None or not pkg.payload:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no comparison payload for this item")

    payload = pkg.payload
    selected_quote_id = payload.get("selected_quote_id")
    if not selected_quote_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no quote has been selected on this item yet — call "
            "/quote-comparison/{item_id}/select/{quote_id} first",
        )
    quote = next(
        (q for q in payload.get("quotes", []) if q.get("quote_id") == selected_quote_id), None
    )
    if quote is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "selected quote not found on this item")

    panel = load_carrier_panel(_CARRIER_PANEL_WORKFLOW_N)
    carrier_name = quote.get("carrier_name")
    profile = next((p for p in panel if p.carrier_name == carrier_name), None)

    return {
        "submission_id": payload.get("submission_id"),
        "named_insured": payload.get("named_insured"),
        "carrier_id": profile.carrier_id if profile else None,
        "carrier_name": carrier_name,
        "selected_quote_id": selected_quote_id,
        "bind_terms_requested": {
            "premium": quote.get("premium"),
            "limits": quote.get("limits"),
            "deductibles": quote.get("deductibles"),
            "effective_date": quote.get("effective_date"),
        },
        "subjectivities_at_selection": [
            {"description": s.get("description"), "materiality": s.get("materiality")}
            for s in quote.get("subjectivities", [])
        ],
    }


def instruction_from_stored_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Reshapes an existing, already-stored ``BindCoordinationPayload`` dict
    back into the ``broker_bind_instruction``-shaped dict
    ``_decide_pre_bind_stage`` expects — lets a later real confirmation/
    issued-policy attach re-derive the bind's full state from the item's own
    current, canonical record rather than re-deriving from Quote Comparison
    again (which the broker may have moved on from by then).

    ``pre_bind_subjectivities`` already matches ``classify_subjectivities``'s
    expected input keys 1:1 (``description``/``materiality``/``status``/
    ``lifecycle_stage``) — only ``requested_bind_terms`` needs reshaping,
    from its flat output shape back into ``bind_terms_from_dict``'s nested
    ``deductibles`` input shape."""
    terms = payload.get("requested_bind_terms") or {}
    return {
        "submission_id": payload.get("submission_id"),
        "named_insured": payload.get("named_insured"),
        "carrier_id": payload.get("carrier_id"),
        "carrier_name": payload.get("carrier_name"),
        "bind_terms_requested": {
            "premium": terms.get("premium"),
            "limits": terms.get("limits"),
            "deductibles": {
                "all_perils": terms.get("deductible_all_perils"),
                "wind_hail": terms.get("deductible_wind_hail"),
            },
            "effective_date": terms.get("effective_date"),
        },
        "subjectivities_at_selection": payload.get("pre_bind_subjectivities") or [],
    }


def _require_live_connector(session: AsyncSession) -> LiveNangoConnectorService:
    connector = build_connector_service(session=session)
    if not isinstance(connector, LiveNangoConnectorService):
        raise ConnectorNotConnectedError(get_settings().nango_integration_mail)
    return connector


async def discover_live_bind_messages(
    session: AsyncSession, ctx: Ctx, named_insured: str | None
) -> list[dict[str, Any]]:
    """Real Gmail messages that could be this bind's carrier confirmation OR
    its eventual issued policy — same inbox search either way (by the bind's
    own real named insured), since a human picking a message already knows
    which one they're looking at; the two different "attach" actions decide
    how it gets parsed. Returns an empty list (not an error) if there's no
    real named insured to search for yet."""
    if not named_insured:
        return []
    connector = _require_live_connector(session)
    messages = await connector.fetch_inbox(ctx, since_cursor=f'subject:"{named_insured}"')
    return [{"id": m.id, "subject": m.subject} for m in messages]


async def save_live_bind_confirmation(
    session: AsyncSession, ctx: Ctx, item_id: str, message_id: str
) -> str:
    """Fetches one real message as plain header+body text (real headers are
    needed for ``parse_bind_confirmation``'s carrier-domain + confirmation-
    date lookups) and persists it under a FIXED filename, keyed by this bind
    item's own id — a bind gets exactly one confirmation, so re-picking a
    different message overwrites it cleanly rather than accumulating like
    Quote Comparison's responses."""
    connector = _require_live_connector(session)
    text = await connector.fetch_email_as_text(ctx, message_id)
    await LocalDocumentStore().save(
        session, ctx, item_id,
        RawDocument(kind=DocumentKind.EMAIL, filename=_CONFIRMATION_FILENAME, content=text),
    )
    return text


async def save_live_issued_policy(
    session: AsyncSession, ctx: Ctx, item_id: str, message_id: str
) -> str:
    """Fetches one real message and prefers its first real attachment's
    already-extracted text — a declarations page realistically arrives as a
    PDF attachment, same as an ACORD form in Market Matching — falling back
    to the plain email body if the carrier pasted the declarations inline
    instead. Persisted under a fixed filename the same overwrite way as the
    confirmation above."""
    connector = _require_live_connector(session)
    bundle = await connector.to_raw_bundle(ctx, message_id)
    text = bundle.documents[0].content if bundle.documents else (bundle.email_body or "")
    await LocalDocumentStore().save(
        session, ctx, item_id,
        RawDocument(kind=DocumentKind.OTHER, filename=_ISSUED_POLICY_FILENAME, content=text),
    )
    return text


async def load_live_bind_confirmation_text(
    session: AsyncSession, ctx: Ctx, item_id: str
) -> str | None:
    """Re-reads the persisted confirmation text — needed again whenever the
    issued policy is attached later, since ``_decide_pre_bind_stage`` must
    see a real confirmation on EVERY re-run or it short-circuits back to
    ``READY`` (see ``run_live_update``'s docstring in ``service.py``)."""
    docs = await LocalDocumentStore().list_for_submission(session, ctx, item_id)
    doc = next((d for d in docs if d.filename == _CONFIRMATION_FILENAME), None)
    return doc.content if doc else None
