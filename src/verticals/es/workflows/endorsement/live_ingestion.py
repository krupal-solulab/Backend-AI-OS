"""Additive live-ingestion path for Endorsement / Mid-Term Change Processing:
starts a real request from an ACTUAL, already-SENT Binder & Policy Issuance
bind record — matching FR-1's "matching on named insured + carrier + bind
ID" — instead of the static Workflow_15 fixture (``scenario_loader.py``,
untouched by this module).

The change TYPE is deliberately BROKER-SUPPLIED, not inferred from the real
retail-agent email: ``classification_engine.classify()`` has always taken
``change_type`` as a plain given input (never derived from text anywhere in
this codebase), and the fixture dataset's own raw emails are free-flowing
natural language with no reliable type signal a regex could extract without
real risk of silently misclassifying a material change as routine — exactly
the failure mode the PRD calls out as most serious (Section 2.3). So this
module has no "parse the change type" function; the broker picks it from the
same taxonomy already in ``classification_engine.py``.

``current_terms`` here never includes ``expiration_date`` — Binder &
Issuance's own output schema has no policy-expiration field at all (out of
that workflow's scope) — proration simply won't compute without it, exactly
the same honest-gap handling ``service.py``'s existing code already does for
a missing expiration date, not something this module works around.
"""

from __future__ import annotations

import json
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
from verticals.es.workflows.binder_issuance.live_ingestion import (
    discover_live_bind_messages as discover_live_messages,
)

_CONTEXT_FILENAME = "bound_policy_context.json"
_ISSUED_ENDORSEMENT_FILENAME = "carrier_issued_endorsement.txt"


def _require_live_connector(session: AsyncSession) -> LiveNangoConnectorService:
    connector = build_connector_service(session=session)
    if not isinstance(connector, LiveNangoConnectorService):
        raise ConnectorNotConnectedError(get_settings().nango_integration_mail)
    return connector

__all__ = [
    "build_bound_policy_context_from_binder",
    "discover_live_messages",
    "save_live_issued_endorsement",
    "save_live_bound_policy_context",
    "load_live_bound_policy_context",
]


async def build_bound_policy_context_from_binder(
    session: AsyncSession, ctx: Ctx, binder_item_id: str, change_type: str, change_detail: str
) -> dict[str, Any]:
    """The live-data equivalent of a ``bound_policy_context.json`` fixture,
    built from a real, already-SENT Binder & Policy Issuance bind."""
    item = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.id) == binder_item_id,
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.workflow) == "binder_issuance",
            )
        )
    ).scalar_one_or_none()
    if item is None or not item.output_package_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no binder-issuance review item '{binder_item_id}'"
        )
    pkg = (
        await session.execute(
            select(OutputPackageRow).where(col(OutputPackageRow.id) == item.output_package_id)
        )
    ).scalar_one_or_none()
    if pkg is None or not pkg.payload:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no bind coordination payload for this item")

    payload = pkg.payload
    if payload.get("bind_order_status") != "SENT":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this bind isn't SENT yet (no carrier confirmation attached) — an endorsement "
            "only applies to an already-bound policy",
        )

    confirmed = (payload.get("carrier_confirmation") or {}).get("confirmed_terms") or {}
    return {
        # Binder & Issuance's OWN stable bind_id (per this session's bind_id
        # stability fix) — NOT this binder-issuance review item's own id.
        # Renewal Remarketing cross-references endorsement history by this
        # exact value (payload["bind_id"] on both sides), so the two must
        # agree; falls back to the review item id only in the unexpected
        # case a bind payload predates that field entirely.
        "bind_id": payload.get("bind_id") or binder_item_id,
        "named_insured": payload.get("named_insured"),
        "carrier_id": payload.get("carrier_id"),
        "carrier_name": payload.get("carrier_name", ""),
        "current_terms": {
            "premium": confirmed.get("premium"),
            "limits": confirmed.get("limits"),
            "effective_date": confirmed.get("effective_date"),
            "expiration_date": confirmed.get("expiration_date"),
        },
        "requested_change": {"type": change_type, "detail": change_detail},
    }


async def save_live_issued_endorsement(
    session: AsyncSession, ctx: Ctx, item_id: str, message_id: str
) -> str:
    """Fetches one real message as plain header+body text (real headers are
    needed for ``parse_issued_endorsement``'s carrier-domain lookup) and
    persists it under a fixed filename, keyed by this endorsement request's
    own id — one issued endorsement per request, so re-picking a different
    message overwrites cleanly, same convention as Binder & Issuance's
    confirmation/policy documents."""
    connector: LiveNangoConnectorService = _require_live_connector(session)
    text = await connector.fetch_email_as_text(ctx, message_id)
    await LocalDocumentStore().save(
        session, ctx, item_id,
        RawDocument(kind=DocumentKind.EMAIL, filename=_ISSUED_ENDORSEMENT_FILENAME, content=text),
    )
    return text


async def save_live_bound_policy_context(
    session: AsyncSession, ctx: Ctx, item_id: str, context: dict[str, Any]
) -> None:
    """Persists the REAL context this request started from — needed again
    whenever the issued endorsement is attached later, since reconstructing
    from the item's own STORED OUTPUT payload would be lossy here
    (``current_terms`` never appears in ``EndorsementRequestPayload`` at
    all) — same "persist the real source, re-derive every time" philosophy
    Quote Comparison already established, just for a JSON context instead
    of an email body."""
    await LocalDocumentStore().save(
        session, ctx, item_id,
        RawDocument(kind=DocumentKind.OTHER, filename=_CONTEXT_FILENAME, content=json.dumps(context)),
    )


async def load_live_bound_policy_context(
    session: AsyncSession, ctx: Ctx, item_id: str
) -> dict[str, Any] | None:
    docs = await LocalDocumentStore().list_for_submission(session, ctx, item_id)
    doc = next((d for d in docs if d.filename == _CONTEXT_FILENAME), None)
    return json.loads(doc.content) if doc else None
