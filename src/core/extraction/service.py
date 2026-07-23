"""Classification + cited extraction over ``Key: Value`` plain text.

Phase-2 tuning (additive, no contract change):
- Repeating rows (multi-claim loss runs, multi-location SOVs) are grouped into LIST
  fields (``loss_run.claims``, ``sov.locations``) instead of silently collapsing to the
  last row.
- Canonical aggregates are exposed under ONE stable name each: ``loss_run.total_incurred``
  (+ ``loss_run.total_incurred_period``), ``loss_run.total_paid``, and
  ``sov.total_insurable_value`` (sum across locations) — so rules never pick building-vs-
  contents or chase a varying ``total_incurred_5yr``/``_2yr`` suffix.
- Numeric coercion strips ``$``, commas, ``%``, ``+`` and trailing ``(...)`` qualifiers.
- A confidence heuristic lowers ``confidence`` on values with illegibility markers so the
  MGA decision core can route degraded scans to manual review (EC-01).
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from core.common.dtos import (
    Citation,
    Ctx,
    ExtractedModel,
    ExtractedValue,
    RawBundle,
    RawDocument,
)
from core.common.enums import DocumentKind

# ``Label: value`` line — label is letters/spaces/&/-/(), value is the rest of the line.
_KV = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /_&()\-\.]{1,60}?)\s*:\s*(\S.*?)\s*$")

# Content keywords per kind, checked in priority order (first hit wins).
_CONTENT_SIGNALS: list[tuple[DocumentKind, tuple[str, ...]]] = [
    (DocumentKind.ACORD, ("acord", "commercial insurance application")),
    (DocumentKind.LOSS_RUN, ("loss run", "loss history", "claim number", "date of loss")),
    (DocumentKind.SOV, ("schedule of values", "sov", "building value", "cope")),
    (DocumentKind.FINANCIALS, ("balance sheet", "income statement", "annual revenue",
                               "total assets", "financial statement")),
    (DocumentKind.EMAIL, ("subject:", "from:", "to:")),
]

# Filename-stem fallback (mirrors the fixtures loader's mapping).
_KIND_BY_STEM: dict[str, DocumentKind] = {
    "acord_application": DocumentKind.ACORD,
    "loss_run": DocumentKind.LOSS_RUN,
    "financial_statement": DocumentKind.FINANCIALS,
    "sov_report": DocumentKind.SOV,
    "sov": DocumentKind.SOV,
    "email": DocumentKind.EMAIL,
}

# Values containing any of these markers are treated as low-confidence (degraded scan).
_LOW_CONF_MARKERS: tuple[str, ...] = (
    "illegible", "[?]", "unclear", "partially", "cut off", "cut-off",
    "unable to confirm", "not clearly readable", "second digit", "month digit",
)
_LOW_CONFIDENCE = 0.4
_HIGH_CONFIDENCE = 1.0

# Repeating (per-row) keys — grouped into list fields rather than collapsed.
_CLAIM_KEYS = frozenset(
    {"claim_number", "date_of_loss", "cause_of_loss", "status", "incurred", "paid", "reserve"}
)
_LOCATION_KEYS = frozenset(
    {"address", "building_value", "contents_value", "business_income_12mo",
     "total_insurable_value", "year_built", "construction_type", "number_of_stories",
     "sprinklered", "roof_type_age", "protection_class", "flood_zone", "distance_to_coast"}
)


def _normalize_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def coerce_number(value: Any) -> float | None:
    """Coerce a currency/percent/qualified string to float, else None.

    Strips ``$ , % +`` and any trailing ``(...)`` qualifier (e.g. ``$680,000 (rental
    income)`` → 680000.0). Ranges / non-numeric (``approx $20,100-$28,100``) → None.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if value is None:
        return None
    text = str(value).split("(", 1)[0]  # drop trailing qualifier
    text = re.sub(r"[,$%\s+]", "", text)
    try:
        return float(text)
    except ValueError:
        return None


def _confidence(value: str) -> float:
    low = value.lower()
    return _LOW_CONFIDENCE if any(m in low for m in _LOW_CONF_MARKERS) else _HIGH_CONFIDENCE


class ExtractionService(Protocol):
    async def classify(self, ctx: Ctx, doc: RawDocument) -> DocumentKind: ...
    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel: ...


class DefaultExtractionService:
    """Filename + content-signal classifier and a cited ``Key: Value`` extractor."""

    async def classify(self, ctx: Ctx, doc: RawDocument) -> DocumentKind:
        if doc.kind is not DocumentKind.OTHER:
            return doc.kind
        text = (doc.content or "").lower()
        for kind, signals in _CONTENT_SIGNALS:
            if any(sig in text for sig in signals):
                return kind
        stem = re.sub(r"\.[^.]+$", "", (doc.filename or "").lower())
        return _KIND_BY_STEM.get(stem, DocumentKind.OTHER)

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        fields: list[ExtractedValue] = []
        seen_kinds: set[DocumentKind] = set()

        for doc in raw.documents:
            kind = await self.classify(ctx, doc)
            seen_kinds.add(kind)
            cite = Citation(document_kind=kind, filename=doc.filename)
            kvs = self._parse_kv(doc.content or "")
            repeating = (_CLAIM_KEYS if kind is DocumentKind.LOSS_RUN
                         else _LOCATION_KEYS if kind is DocumentKind.SOV else frozenset())

            rows, scalars = self._split_rows(kvs, repeating)

            # Scalar fields (one per key; namespaced by kind, cited to their line).
            for leaf, (value, lineno) in scalars.items():
                fields.append(self._value(kind, leaf, value, doc.filename, lineno))

            # Structured list + canonical aggregates for repeating sections.
            if kind is DocumentKind.LOSS_RUN and rows:
                fields.append(ExtractedValue(
                    name="loss_run.claims", value=rows, confidence=_HIGH_CONFIDENCE, citation=cite))
            if kind is DocumentKind.SOV and rows:
                fields.append(ExtractedValue(
                    name="sov.locations", value=rows, confidence=_HIGH_CONFIDENCE, citation=cite))
                agg = sum(n for r in rows
                          if (n := coerce_number(r.get("total_insurable_value"))) is not None)
                fields.append(ExtractedValue(
                    name="sov.total_insurable_value", value=agg,
                    confidence=_HIGH_CONFIDENCE, citation=cite))

            if kind is DocumentKind.LOSS_RUN:
                self._canonical_total(fields, scalars, "total_incurred", cite)
                self._canonical_total(fields, scalars, "total_paid", cite)

        for kind in sorted(seen_kinds, key=lambda k: k.value):
            fields.append(ExtractedValue(
                name=f"documents.{kind.value}.present", value=True, confidence=_HIGH_CONFIDENCE))

        return ExtractedModel(submission_id=raw.submission_id, fields=fields)

    @staticmethod
    def _parse_kv(content: str) -> list[tuple[int, str, str]]:
        out: list[tuple[int, str, str]] = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            m = _KV.match(line)
            if m:
                out.append((lineno, _normalize_key(m.group(1)), m.group(2)))
        return out

    @staticmethod
    def _split_rows(
        kvs: list[tuple[int, str, str]], repeating: frozenset[str]
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[str, int]]]:
        """Split parsed pairs into grouped repeating rows + scalar (header) fields.

        A new row starts whenever a repeating key reappears within the current row."""
        rows: list[dict[str, Any]] = []
        scalars: dict[str, tuple[str, int]] = {}
        current: dict[str, Any] = {}
        for lineno, leaf, value in kvs:
            if leaf in repeating:
                if leaf in current:
                    rows.append(current)
                    current = {}
                current[leaf] = value
            else:
                scalars.setdefault(leaf, (value, lineno))
        if current:
            rows.append(current)
        return rows, scalars

    @staticmethod
    def _value(
        kind: DocumentKind, leaf: str, value: str, filename: str, lineno: int
    ) -> ExtractedValue:
        return ExtractedValue(
            name=f"{kind.value}.{leaf}",
            value=value,
            confidence=_confidence(value),
            citation=Citation(document_kind=kind, filename=filename, locator=f"line {lineno}"),
        )

    @staticmethod
    def _canonical_total(
        fields: list[ExtractedValue], scalars: dict[str, tuple[str, int]],
        base: str, cite: Citation,
    ) -> None:
        """Emit ``loss_run.<base>`` (+ ``_period``) from a varying ``<base>_<period>`` key."""
        for leaf, (value, _lineno) in scalars.items():
            if leaf.startswith(base + "_"):
                period = leaf[len(base) + 1:]
                num = coerce_number(value)
                fields.append(ExtractedValue(
                    name=f"loss_run.{base}", value=num if num is not None else value,
                    confidence=_confidence(value), citation=cite))
                fields.append(ExtractedValue(
                    name=f"loss_run.{base}_period", value=period,
                    confidence=_HIGH_CONFIDENCE, citation=cite))
                return
