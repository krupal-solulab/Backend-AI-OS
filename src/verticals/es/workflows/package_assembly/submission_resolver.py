"""Resolves a scenario's ``named_insured`` back to its real Workflow_10
submission and re-derives that submission's ``ExtractedModel``.

Why this exists (see Validation_Rules_Test_Dataset.md's "Data provenance
note"): ``market_matching_output.json`` carries the carrier selection and
requirements, but NOT field-level extracted values — e.g. Scenario 04 needs
the real SOV's ``Year Built`` / ``Construction`` / etc. to know what can be
auto-filled, and those simply aren't inlined in the scenario JSON. Market
Matching never persists its ``ExtractedModel`` anywhere queryable, so the
only way to get real, citable field values is to re-derive them from the
same underlying documents via the same shared extraction service — a
deterministic re-materialization of already-extracted data, not a new
extraction judgment. This is the one place this workflow's "no
re-extraction" boundary (PRD FR-1) is knowingly stretched, and it's
confined entirely to this function.
"""

from __future__ import annotations

import re

from core.common.dtos import Ctx, ExtractedModel, RawBundle, RawDocument
from core.common.enums import Vertical
from core.extraction import ExtractionService
from fixtures import load_workflow

MARKET_MATCHING_WORKFLOW_N = 10


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.strip().lower()).strip()


async def resolve_extracted_model(
    ctx: Ctx,
    named_insured: str,
    extraction: ExtractionService,
    *,
    workflow_n: int = MARKET_MATCHING_WORKFLOW_N,
) -> ExtractedModel:
    """Scans every Workflow_10 submission for a matching ``acord.named_insured``
    and returns its ExtractedModel. Raises ``LookupError`` if none match —
    a scenario referencing a named insured with no underlying submission is a
    data problem worth failing loudly on, not silently proceeding without
    grounding data."""
    target = _normalize(named_insured)
    loaded = load_workflow(workflow_n, tenant_id=ctx.tenant_id, vertical=Vertical.ES)

    for ls in loaded:
        raw = RawBundle(
            submission_id=ls.submission.external_ref,
            documents=[
                RawDocument(kind=d.kind, filename=d.filename, content=d.content or "", uri=d.uri)
                for d in ls.documents
            ],
        )
        model = await extraction.extract(ctx, raw)
        candidate = next((f.value for f in model.fields if f.name == "acord.named_insured"), None)
        if candidate is not None and _normalize(str(candidate)) == target:
            return model

    raise LookupError(
        f"no Workflow_{workflow_n} submission found for named insured '{named_insured}'"
    )
