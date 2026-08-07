"""Additive real-ingestion path for Renewal Remarketing (FR-2): builds a
``renewal_context.json``-shaped dict from an ACTUAL Binder Issuance bind
record plus real Endorsement Processing history for that same bind, instead
of the static Workflow_16 fixture (``scenario_loader.py``, untouched by this
module) — the genuine "current, accurate account profile" hand-off FR-2
always described: "combining the original bind record... with any
subsequent endorsement history... exposure changes already reflected in an
endorsement must not be re-treated as new information at renewal."

Real data status per RR rule, as of the gap-fill pass (all confirmed
against the real schemas, not assumed):
- **RR-01 (exposure change)**: Endorsement Processing's `classify()` already
  computes a real `percent_change` for `employee_count_update` endorsements
  (previously computed and silently discarded — now plumbed through to
  `EndorsementRequestPayload.requested_change.percent_change`). When the
  most recent material (`UNDERWRITING_REVIEW_REQUIRED`) endorsement for this
  bind is that type, its real percentage drives `exposure_change.pct_change`
  here. Other material-endorsement types (e.g. `limit_increase`) have no
  comparable numeric signal, so `pct_change` honestly falls back to `0.0`
  for those — never a guessed percentage. The `already_endorsed` `note`
  (satisfying `remarket_engine.py`'s existing `_ALREADY_EXPLAINED_PATTERNS`
  regex, unchanged) still fires whenever a material endorsement exists,
  regardless of whether a numeric delta was available.
- **RR-02 (loss history)** has no source anywhere in this codebase — no
  claims/loss-run workflow exists in the bind→endorsement→renewal chain.
  `expiring_term_loss_activity` stays `""`, which means `FULL_REMARKET`
  (requiring `loss.trend == "worsening"`) remains unreachable from pure
  real data. This is the one genuinely unclosed part of the gap.
- **RR-03/RR-07 (incumbent offer / non-response)**: now genuinely real.
  Binder & Issuance's bind terms carry a real (explicitly assumed-default,
  12-month-term) `expiration_date` (see its `schema.py`); once that exists,
  `_check_incumbent_offer_status()` searches the connected live inbox for a
  message that actually parses as a renewal offer (a real premium). Found →
  `received=True` with a real `days_before_expiration`/`pct_premium_change`.
  Not found → `received=False` with a real `days_before_expiration_at_check`,
  letting `check_incumbent_status()`'s existing, unmodified non-response
  threshold genuinely fire `URGENT_REMARKET`.
- **RR-08 (remarketing history)**: now genuinely real — `_real_remarketing_
  history()` reads this workflow's own real prior trigger-stage reviews for
  the same named insured (a different real bind_id per real renewal cycle),
  never a hand-authored or simulated history.
- All of the above still resolve through `remarket_engine.py`'s existing,
  completely unmodified rule functions — this module only ever supplies
  better real inputs, never new decision logic.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
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
from verticals.es.workflows.binder_issuance.live_ingestion import (
    discover_live_bind_messages as discover_live_messages,
)
from verticals.es.workflows.renewal_remarketing.incumbent_offer_parser import (
    parse_incumbent_renewal_offer,
)

_INCUMBENT_OFFER_FILENAME = "incumbent_renewal_offer.txt"
_MONEY_RE = re.compile(r"\d[\d,]*")


def _parse_money(value: Any) -> float | None:
    """Quote Comparison's own ``deductibles.all_perils`` is a DISPLAY
    string (e.g. ``"$15,000"``), not a number — ``ComparisonOptionOut``
    needs a real float, same conversion already done in every other native
    parser in this vertical (``bind_parser.parse_money`` etc.)."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    match = _MONEY_RE.search(str(value))
    return float(match.group(0).replace(",", "")) if match else None


def _require_live_connector(session: AsyncSession) -> LiveNangoConnectorService:
    connector = build_connector_service(session=session)
    if not isinstance(connector, LiveNangoConnectorService):
        raise ConnectorNotConnectedError(get_settings().nango_integration_mail)
    return connector


async def _binder_issuance_rows(session: AsyncSession, ctx: Ctx) -> list[OutputPackageRow]:
    rows = (
        await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.workflow) == "binder_issuance",
            )
        )
    ).scalars().all()
    return list(rows)


async def discover_live_binds(session: AsyncSession, ctx: Ctx) -> list[dict[str, Any]]:
    """Every real Binder Issuance bind for this tenant — so a broker never
    has to already know a bind_id to check it."""
    rows = await _binder_issuance_rows(session, ctx)
    binds = []
    for row in rows:
        if not row.payload or not row.payload.get("bind_id"):
            continue
        binds.append({
            "bind_id": row.payload["bind_id"],
            "named_insured": row.payload.get("named_insured"),
            "carrier_name": row.payload.get("carrier_name"),
        })
    return binds


def _bind_expiration_and_premium(bind_payload: dict[str, Any]) -> tuple[date | None, float | None]:
    """Prefers the carrier-confirmed terms (authoritative once bound) over
    the originally-requested terms, same precedence as everywhere else this
    payload is read. ``expiration_date`` here is always the assumed-default
    12-month term computed by binder_issuance's ``_terms_out()`` — never
    treated as more certain than that upstream flag already says it is."""
    confirmed = (bind_payload.get("carrier_confirmation") or {}).get("confirmed_terms") or {}
    requested = bind_payload.get("requested_bind_terms") or {}
    terms = confirmed if confirmed.get("expiration_date") else requested
    expiration_raw = terms.get("expiration_date")
    expiration = date.fromisoformat(expiration_raw) if expiration_raw else None
    premium = terms.get("premium")
    return expiration, premium


def _parse_message_date(raw_text: str) -> date | None:
    match = re.search(r"^Date:\s*(.+)$", raw_text, re.MULTILINE)
    if not match:
        return None
    try:
        return parsedate_to_datetime(match.group(1).strip()).date()
    except (TypeError, ValueError, IndexError):
        return None


async def _check_incumbent_offer_status(
    session: AsyncSession,
    ctx: Ctx,
    named_insured: str | None,
    expiration_date: date | None,
    bind_premium: float | None,
) -> dict[str, Any]:
    """RR-03/RR-07 at trigger time: a best-effort REAL check for whether the
    incumbent has already sent renewal terms, searching the connected inbox
    for a message that actually parses as one (extracts a real premium) —
    never a fabricated received/non-response state. Returns ``{}`` (the
    prior, honest "no signal" default) when there's no real expiration date
    to measure against yet, or no live connector — a real gmail-connection
    requirement here is new, but degrading gracefully rather than raising
    keeps "Check live renewal" itself working even without it."""
    if expiration_date is None:
        return {}
    try:
        connector = _require_live_connector(session)
    except ConnectorNotConnectedError:
        return {}
    today = datetime.now(UTC).date()
    candidates = await discover_live_messages(session, ctx, named_insured)
    for candidate in candidates[:5]:
        text = await connector.fetch_email_as_text(ctx, candidate["id"])
        parsed = parse_incumbent_renewal_offer(text)
        if parsed.premium is None:
            continue
        message_date = _parse_message_date(text) or today
        pct_premium_change = (
            (parsed.premium - bind_premium) / bind_premium * 100.0 if bind_premium else None
        )
        return {
            "received": True,
            "days_before_expiration": (expiration_date - message_date).days,
            "pct_premium_change": pct_premium_change,
        }
    return {
        "received": False,
        "days_before_expiration_at_check": (expiration_date - today).days,
    }


async def _real_remarketing_history(
    session: AsyncSession, ctx: Ctx, named_insured: str | None, exclude_bind_id: str
) -> list[dict[str, Any]] | None:
    """RR-08/FR-10: this workflow's own real prior output IS the
    remarketing-history data — reads back real trigger-stage reviews for
    this same named insured from other real cycles (a different bind_id —
    each real renewal cycle in this dataset corresponds to a distinct real
    Binder & Issuance bind), never a hand-authored or simulated history.
    Returns ``None`` (not an empty list) when no prior real cycle exists
    yet, so ``parse_remarketing_history``'s FR-11 no-suppression default
    applies exactly like the fixture path."""
    if not named_insured:
        return None
    rows = (
        await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.workflow) == "renewal_remarketing",
            )
        )
    ).scalars().all()
    same_account = [r for r in rows if r.payload and r.payload.get("named_insured") == named_insured]
    comparison_rows = [r for r in same_account if r.payload.get("is_comparison_stage")]
    # Pydantic's model_dump() always includes every field, so a comparison-
    # stage row still has a (placeholder, "NO_REMARKET") trigger_decision
    # key — is_comparison_stage is the real discriminator between a genuine
    # trigger-stage cycle and a comparison-stage sub-record.
    cycles = [
        r
        for r in same_account
        if r.payload.get("bind_id") != exclude_bind_id and not r.payload.get("is_comparison_stage")
    ]
    if not cycles:
        return None

    history = []
    for row in sorted(cycles, key=lambda r: r.created_at):
        payload = row.payload
        savings = None
        comparison = next(
            (c for c in comparison_rows if c.payload.get("bind_id") == payload.get("bind_id")),
            None,
        )
        if comparison is not None:
            comp_out = (comparison.payload.get("remarket_execution") or {}).get(
                "comparison_output"
            ) or {}
            incumbent = comp_out.get("incumbent") or {}
            alternative = comp_out.get("alternative") or {}
            if incumbent.get("premium") is not None and alternative.get("premium") is not None:
                diff = incumbent["premium"] - alternative["premium"]
                savings = diff if diff > 0 else None
        history.append({
            "cycle_year": row.created_at.year,
            "trigger_level": (payload.get("trigger_decision") or {}).get("level"),
            "remarketed": bool((payload.get("remarket_execution") or {}).get("initiated")),
            "savings_identified": savings,
        })
    return history


async def build_live_renewal_context(
    session: AsyncSession, ctx: Ctx, bind_id: str
) -> dict[str, Any]:
    """The live-data equivalent of ``scenario_loader.load_scenario()``'s
    trigger-stage ``renewal_context.json`` shape, built from a real bind
    plus real endorsement history for it."""
    binder_rows = await _binder_issuance_rows(session, ctx)
    bind_payload = next(
        (r.payload for r in binder_rows if r.payload and r.payload.get("bind_id") == bind_id),
        None,
    )
    if bind_payload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no binder-issuance bind '{bind_id}'")

    named_insured = bind_payload.get("named_insured")
    endorsement_rows = (
        await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.workflow) == "endorsement",
                col(OutputPackageRow.submission_id) == bind_id,
            )
        )
    ).scalars().all()
    material_endorsement_rows = sorted(
        (
            r
            for r in endorsement_rows
            if r.payload and r.payload.get("classification") == "UNDERWRITING_REVIEW_REQUIRED"
        ),
        key=lambda r: r.created_at,
        reverse=True,
    )
    material_endorsements = [r.payload for r in material_endorsement_rows]

    if material_endorsements:
        most_recent = material_endorsements[0].get("requested_change", {})
        detail = most_recent.get("detail", "")
        # Real, already-computed by Endorsement Processing (employee_count_
        # update only) — falls back to 0.0 honestly for endorsement types
        # with no comparable numeric signal (e.g. limit_increase), same as
        # before, never invented.
        pct_change = most_recent.get("percent_change") or 0.0
        note = (
            f"This exposure change was already endorsed mid-term "
            f"({detail}) — see Endorsement Processing record for this bind."
        )
    else:
        pct_change = 0.0
        note = None

    expiration_date, bind_premium = _bind_expiration_and_premium(bind_payload)
    incumbent_status = await _check_incumbent_offer_status(
        session, ctx, named_insured, expiration_date, bind_premium
    )
    remarketing_history = await _real_remarketing_history(
        session, ctx, named_insured, exclude_bind_id=bind_id
    )

    return {
        "bind_id": bind_id,
        "named_insured": named_insured,
        "incumbent_carrier_id": bind_payload.get("carrier_id"),
        "incumbent_carrier_name": bind_payload.get("carrier_name", ""),
        "exposure_change": {"pct_change": pct_change, "note": note},
        "expiring_term_loss_activity": "",
        "incumbent_renewal_offer": incumbent_status,
        "remarketing_history": remarketing_history,
    }


async def save_live_incumbent_offer(
    session: AsyncSession, ctx: Ctx, item_id: str, message_id: str
) -> str:
    """Fetches one real message as plain header+body text and persists it
    under a fixed filename, keyed by this renewal review's own id — a
    renewal gets exactly one incumbent offer per cycle, so re-picking a
    different message overwrites cleanly, same convention as Binder &
    Issuance's confirmation/policy documents."""
    connector = _require_live_connector(session)
    text = await connector.fetch_email_as_text(ctx, message_id)
    await LocalDocumentStore().save(
        session, ctx, item_id,
        RawDocument(kind=DocumentKind.EMAIL, filename=_INCUMBENT_OFFER_FILENAME, content=text),
    )
    return text


async def find_live_alternative_quote(
    session: AsyncSession, ctx: Ctx, named_insured: str | None
) -> dict[str, Any] | None:
    """The real "remarketed alternative" side of RR-06's comparison — reuses
    an ALREADY-REAL, already-selected Quote Comparison quote for this same
    named insured rather than inventing a second live-ingestion path for
    "the alternative quote." Same "reuse a real upstream workflow's own
    output" precedent as Binder & Issuance pulling its terms from Quote
    Comparison and Endorsement pulling its context from Binder & Issuance.
    Returns ``None`` (never fabricated) if no selected quote exists yet for
    this account — the broker needs to run a real remarket + quote
    comparison first."""
    if not named_insured:
        return None
    rows = (
        await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.workflow) == "quote_comparison",
            )
        )
    ).scalars().all()
    for row in rows:
        payload = row.payload or {}
        if payload.get("named_insured") != named_insured:
            continue
        selected_id = payload.get("selected_quote_id")
        if not selected_id:
            continue
        quote = next(
            (q for q in payload.get("quotes", []) if q.get("quote_id") == selected_id), None
        )
        if quote is None:
            continue
        deductibles = quote.get("deductibles") or {}
        return {
            "carrier_name": quote.get("carrier_name", ""),
            "premium": quote.get("premium"),
            "limits": quote.get("limits"),
            "deductible": _parse_money(deductibles.get("all_perils")),
            # Quote Comparison's real schema has no "manual underwriting
            # exception" concept at all (that's a fixture-only cover-note
            # phrase in Scenario 05) — left honestly empty rather than
            # populated with subjectivities that would never actually make
            # compare_renewal_options's exception regex match, which could
            # misleadingly read as "checked, and it's not an exception."
            "note": "",
        }
    return None
