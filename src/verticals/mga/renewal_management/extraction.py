"""Renewal extraction — reuse the shared Extraction Core for recognized documents
(loss run / financials / email / SOV) and parse the two renewal-specific documents
(prior_policy_snapshot, renewal_questionnaire) locally, namespaced ``prior_policy.*`` /
``renewal_questionnaire.*``.

Keeps ``DocumentKind`` frozen: the renewal docs classify as ``OTHER`` at the shared layer,
so we parse them here and distinguish by filename in their citations. Tolerant of variable
doc sets (missing/extra docs never error).
"""

from __future__ import annotations

import re
from pathlib import Path

from core.common.dtos import Citation, Ctx, ExtractedModel, ExtractedValue, RawBundle, RawDocument
from core.common.enums import DocumentKind
from core.extraction import DefaultExtractionService

# filename stem → local namespace for the renewal-specific documents
_RENEWAL_DOCS: dict[str, str] = {
    "prior_policy_snapshot": "prior_policy",
    "renewal_questionnaire": "renewal_questionnaire",
}

# Renewal docs mix "Label: value" and questionnaire-style "Question? answer" lines.
_KV = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /_&()\-\.]{1,70}?)\s*:\s*(\S.*?)\s*$")
_QA = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /_&()\-\.]{1,70}?)\?\s+(\S.*?)\s*$")


def _stem(filename: str) -> str:
    return Path(filename).stem.lower()


def _norm(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


class RenewalExtractionService:
    """Composes shared extraction + a local parser for the renewal-specific docs."""

    def __init__(self) -> None:
        self._shared = DefaultExtractionService()

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        renewal_docs = [d for d in raw.documents if _stem(d.filename) in _RENEWAL_DOCS]
        shared_docs = [d for d in raw.documents if _stem(d.filename) not in _RENEWAL_DOCS]

        fields: list[ExtractedValue] = []
        if shared_docs:
            shared_model = await self._shared.extract(
                ctx, RawBundle(submission_id=raw.submission_id, documents=shared_docs)
            )
            fields.extend(shared_model.fields)

        for doc in renewal_docs:
            prefix = _RENEWAL_DOCS[_stem(doc.filename)]
            fields.extend(self._parse_doc(doc, prefix))
            fields.append(ExtractedValue(name=f"documents.{prefix}.present", value=True))

        return ExtractedModel(submission_id=raw.submission_id, fields=fields)

    @staticmethod
    def _parse_doc(doc: RawDocument, prefix: str) -> list[ExtractedValue]:
        out: list[ExtractedValue] = []
        for lineno, line in enumerate((doc.content or "").splitlines(), start=1):
            m = _KV.match(line) or _QA.match(line)
            if not m:
                continue
            out.append(
                ExtractedValue(
                    name=f"{prefix}.{_norm(m.group(1))}",
                    value=m.group(2),
                    confidence=1.0,
                    citation=Citation(
                        document_kind=DocumentKind.OTHER, filename=doc.filename,
                        locator=f"line {lineno}",
                    ),
                )
            )
        return out
