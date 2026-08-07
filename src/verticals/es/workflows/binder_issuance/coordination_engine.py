"""Native BI-01..BI-07 coordination logic. None of this fits the generic
6-check rules engine — it's compound lifecycle/reconciliation reasoning,
Option-A precedent applied a fourth time.

The core principle underneath every rule here (per
RULE_ENGINE_INTERPRETATION_GUIDE.md): a carrier's own bind confirmation or
issued policy document is DATA TO VERIFY, never trusted just because it's
the carrier's official output (BI-03/BI-05).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from verticals.es.workflows.binder_issuance.bind_parser import (
    limits_signature,
    parse_date_any,
    parse_money,
)

DEFAULT_ISSUANCE_TIMELINE_DAYS = 45  # FR-9's own placeholder default
REMINDER_INTERVALS_DAYS = (15, 5)  # FR-17's own worked example


@dataclass(frozen=True)
class BindTerms:
    premium: float | None = None
    limits_display: str | None = None
    deductible_all_perils: float | None = None
    deductible_wind_hail: float | None = None
    effective_date: date | None = None


@dataclass(frozen=True)
class Subjectivity:
    description: str
    materiality: str  # routine | material
    lifecycle_stage: str  # PRE_BIND | POST_BIND_ONGOING
    cleared: bool


@dataclass(frozen=True)
class Discrepancy:
    field: str
    requested_or_bound: str
    confirmed_or_issued: str


@dataclass(frozen=True)
class OngoingObligation:
    description: str
    due_date: date | None
    status: str  # open | completed


def classify_subjectivities(raw: list[dict[str, Any]]) -> list[Subjectivity]:
    """FR-3/BI-02/BI-07: inherits materiality + lifecycle stage from Quote
    Comparison's QC-02 classification AS-IS — never re-classified here.
    Absence of ``lifecycle_stage`` means implicitly PRE_BIND (only items
    that need POST_BIND_ONGOING carry the key explicitly in this dataset)."""
    result = []
    for item in raw:
        status_text = str(item.get("status", "")).lower()
        result.append(Subjectivity(
            description=item["description"],
            materiality=item.get("materiality", "routine"),
            lifecycle_stage=item.get("lifecycle_stage") or "PRE_BIND",
            cleared="cleared" in status_text,
        ))
    return result


def blocking_pre_bind_items(subjectivities: list[Subjectivity]) -> list[Subjectivity]:
    """BI-02: any MATERIAL + PRE_BIND item not yet cleared blocks the bind
    order — the only gate this workflow uses (no gap-tolerant middle state
    the way Package Assembly's READY_WITH_GAP was, per FR-4)."""
    return [
        s for s in subjectivities
        if s.materiality == "material" and s.lifecycle_stage == "PRE_BIND" and not s.cleared
    ]


def post_bind_ongoing_obligations(
    subjectivities: list[Subjectivity], effective_date: date | None
) -> list[OngoingObligation]:
    """BI-07: tracks POST_BIND_ONGOING items as persistent, dated tasks —
    never blocks anything. Due date is computed from the description's own
    "within N days" phrasing, anchored to the policy's effective/binding
    date (not the confirmation email's send date) — "within N days" reads
    as "before N full days have elapsed," so the deadline is N-1 days after
    binding, not N (verified against this dataset's own worked example:
    "within 60 days" from an 08/01/2027 binding date -> due 09/29/2027, not
    09/30)."""
    obligations = []
    for s in subjectivities:
        if s.lifecycle_stage != "POST_BIND_ONGOING":
            continue
        due = None
        match = re.search(r"within\s+(\d+)\s+days", s.description, re.IGNORECASE)
        if match and effective_date is not None:
            due = effective_date + timedelta(days=int(match.group(1)) - 1)
        obligations.append(OngoingObligation(
            description=s.description, due_date=due,
            status="completed" if s.cleared else "open",
        ))
    return obligations


def reminder_due(obligation: OngoingObligation, as_of: date) -> bool:
    """FR-17: a reminder is due at the configurable intervals before the
    deadline (15 and 5 days prior, per this dataset's own modeled pattern)."""
    if obligation.due_date is None or obligation.status == "completed":
        return False
    days_remaining = (obligation.due_date - as_of).days
    return any(days_remaining <= interval for interval in REMINDER_INTERVALS_DAYS)


def reconcile(baseline: BindTerms, other: BindTerms) -> list[Discrepancy]:
    """BI-03/BI-05: field-by-field, exact match required — no materiality
    tolerance the way Quote Comparison's QC-01 allowed (FR-7/FR-15: ANY
    mismatch on these fields is a discrepancy, not just a 'looks about
    right' comparison)."""
    diffs: list[Discrepancy] = []
    if baseline.premium != other.premium:
        diffs.append(Discrepancy("premium", _fmt(baseline.premium), _fmt(other.premium)))
    if limits_signature(baseline.limits_display) != limits_signature(other.limits_display):
        diffs.append(Discrepancy(
            "limits", baseline.limits_display or "not stated", other.limits_display or "not stated"
        ))
    if baseline.deductible_all_perils != other.deductible_all_perils:
        diffs.append(Discrepancy(
            "deductible (all perils)",
            _fmt(baseline.deductible_all_perils), _fmt(other.deductible_all_perils),
        ))
    if baseline.deductible_wind_hail != other.deductible_wind_hail:
        diffs.append(Discrepancy(
            "deductible (wind/hail)",
            _fmt(baseline.deductible_wind_hail), _fmt(other.deductible_wind_hail),
        ))
    if baseline.effective_date != other.effective_date:
        diffs.append(Discrepancy(
            "effective_date",
            baseline.effective_date.isoformat() if baseline.effective_date else "not stated",
            other.effective_date.isoformat() if other.effective_date else "not stated",
        ))
    return diffs


def issuance_expected_by(
    bind_confirmed_date: date, stated_timeline_days: int | None
) -> tuple[date, bool]:
    """BI-04: uses the carrier's OWN stated timeline as the threshold where
    given; the configurable default (FR-9) is used otherwise and must be
    flagged as an assumption, not a confirmed carrier commitment (the
    returned bool)."""
    days = (
        stated_timeline_days if stated_timeline_days is not None
        else DEFAULT_ISSUANCE_TIMELINE_DAYS
    )
    return bind_confirmed_date + timedelta(days=days), stated_timeline_days is None


def is_overdue(expected_by: date, as_of: date, documents_received: bool) -> bool:
    return (not documents_received) and as_of > expected_by


def _fmt(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "not stated"


def recompute_live_state(payload: dict[str, Any], as_of: date) -> dict[str, Any]:
    """BI-04/BI-07: recomputes ``overdue_alert_fired`` and each ongoing
    obligation's ``reminder_due`` against the CURRENT date at read time —
    same deferred "no new scheduler" pattern used for Quote Comparison's
    QC-07 (per the approved plan), applied here to a second, third-in-the-
    vertical instance of this problem. Pure projection, no DB write."""
    updated = dict(payload)

    issuance = dict(payload.get("policy_issuance") or {})
    expected_by = issuance.get("expected_by_date")
    if expected_by:
        issuance["overdue_alert_fired"] = is_overdue(
            date.fromisoformat(expected_by), as_of, bool(issuance.get("documents_received"))
        )
        updated["policy_issuance"] = issuance

    obligations = []
    for o in payload.get("post_bind_ongoing_obligations") or []:
        o = dict(o)
        if o.get("due_date") and o.get("status") != "completed":
            due = date.fromisoformat(o["due_date"])
            days_remaining = (due - as_of).days
            o["reminder_due"] = any(days_remaining <= i for i in REMINDER_INTERVALS_DAYS)
        else:
            o["reminder_due"] = False
        obligations.append(o)
    updated["post_bind_ongoing_obligations"] = obligations

    return updated


def bind_terms_from_dict(data: dict[str, Any]) -> BindTerms:
    """Builds ``BindTerms`` from a structured JSON snapshot
    (``broker_bind_instruction.json``'s ``bind_terms_requested`` or
    ``bind_record.json``'s ``bound_terms_confirmed``) — handles both the
    single-``limits``-string shape (GL/Excess-only lines) and the split
    ``property_limit``/``gl_limits`` shape (property+GL combo lines)."""
    limits_display = data.get("limits")
    if limits_display is None and ("property_limit" in data or "gl_limits" in data):
        parts = []
        if data.get("property_limit") is not None:
            parts.append(f"Property ${data['property_limit']:,.0f}")
        if data.get("gl_limits"):
            parts.append(f"GL {data['gl_limits']}")
        limits_display = "; ".join(parts) or None

    deductibles = data.get("deductibles")
    if isinstance(deductibles, dict):
        all_perils = parse_money(deductibles.get("all_perils"))
        wind_hail = parse_money(deductibles.get("wind_hail"))
    else:
        all_perils = parse_money(data.get("deductible"))
        wind_hail = None

    return BindTerms(
        premium=parse_money(data.get("premium")),
        limits_display=limits_display,
        deductible_all_perils=all_perils,
        deductible_wind_hail=wind_hail,
        effective_date=parse_date_any(data.get("effective_date")),
    )
