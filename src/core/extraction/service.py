"""Classification + cited extraction over ``Key: Value`` plain text."""

from __future__ import annotations

import re
from typing import Protocol

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


def _normalize_key(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug


class ExtractionService(Protocol):
    async def classify(self, ctx: Ctx, doc: RawDocument) -> DocumentKind: ...
    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel: ...


class DefaultExtractionService:
    """Filename + content-signal classifier and a cited ``Key: Value`` extractor.

    Field names are namespaced by document kind (e.g. ``acord.named_insured``) so
    values from different documents never collide, and each value cites its source
    document + line. A ``documents.<kind>.present`` flag is emitted per present kind so
    downstream RULES (not this service) can enforce "required document present".
    """

    async def classify(self, ctx: Ctx, doc: RawDocument) -> DocumentKind:
        # An explicit non-OTHER kind on the raw doc is authoritative.
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
            prefix = kind.value
            for lineno, line in enumerate((doc.content or "").splitlines(), start=1):
                m = _KV.match(line)
                if not m:
                    continue
                key, value = m.group(1), m.group(2)
                fields.append(
                    ExtractedValue(
                        name=f"{prefix}.{_normalize_key(key)}",
                        value=value,
                        confidence=1.0,
                        citation=Citation(
                            document_kind=kind,
                            filename=doc.filename,
                            locator=f"line {lineno}",
                        ),
                    )
                )

        # Presence flags (no citation — derived, not extracted from a source line).
        for kind in sorted(seen_kinds, key=lambda k: k.value):
            fields.append(
                ExtractedValue(name=f"documents.{kind.value}.present", value=True, confidence=1.0)
            )

        return ExtractedModel(submission_id=raw.submission_id, fields=fields)
