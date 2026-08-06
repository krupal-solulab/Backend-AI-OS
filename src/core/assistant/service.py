"""Retrieval for the AI Assistant — turns real ``ReviewItem`` rows (joined to
their ``OutputPackage``) into grounding ``ExtractedValue`` facts for
``LLMService.draft()``, and a lightweight cross-workflow overview for the
assistant page's greeting/suggested-prompts.

Deliberately workflow-agnostic: every one of the 10 ES workflows already
writes to these two shared tables, so nothing here imports any workflow
module — a new workflow needs zero changes to show up here.

Note: ``ReviewItem.submission_id``/``OutputPackage.submission_id`` are a
*label*, not a real FK lookup — the ``Submission`` table is never populated
by any live workflow path (each workflow mints its own ref: a real Gmail
message id, a carrier/class key like ``"CAR-05:ALL"``, etc.), so this
module never joins to it.
"""

from __future__ import annotations

import json
import re

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Citation, ExtractedValue
from core.common.enums import DocumentKind, ReviewStatus
from core.models import OutputPackage, ReviewItem

WORKFLOW_LABELS = {
    "market_matching": "Market Matching",
    "package_assembly": "Package Assembly",
    "agent_communication": "Agent Communication",
    "quote_comparison": "Quote Comparison",
    "binder_issuance": "Binder Issuance",
    "endorsement": "Endorsement Processing",
    "renewal_remarketing": "Renewal Remarketing",
    "diligent_search": "Diligent Search",
    "carrier_appetite_intelligence": "Carrier Appetite Intelligence",
    "pipeline_reporting": "Pipeline Reporting",
}

_MAX_FACTS = 25
_MAX_ROWS = 200
_MAX_PAYLOAD_CHARS = 1500
_WORD_RE = re.compile(r"[a-zA-Z0-9]{4,}")


async def _recent_rows(
    session: AsyncSession, tenant_id: str
) -> list[tuple[ReviewItem, OutputPackage]]:
    result = await session.execute(
        select(ReviewItem, OutputPackage)
        .join(OutputPackage, col(ReviewItem.output_package_id) == col(OutputPackage.id))
        .where(col(ReviewItem.tenant_id) == tenant_id)
        .order_by(col(ReviewItem.created_at).desc())
        .limit(_MAX_ROWS)
    )
    return list(result.all())


def _status_value(status: ReviewStatus | str) -> str:
    return status.value if isinstance(status, ReviewStatus) else str(status)


def _row_text(ri: ReviewItem, op: OutputPackage) -> str:
    return f"{ri.submission_id or ''} {json.dumps(op.payload or {}, default=str)}".lower()


async def gather_context(
    session: AsyncSession, tenant_id: str, message: str
) -> list[ExtractedValue]:
    """Real, live facts for one chat turn — filtered to whatever real
    ref/carrier/class/company word the question names, if any word it uses
    actually shows up in a real item, else just the tenant's most recent
    cross-workflow activity."""
    rows = await _recent_rows(session, tenant_id)

    words = _WORD_RE.findall(message.lower())
    if words:
        matched = [(ri, op) for ri, op in rows if any(w in _row_text(ri, op) for w in words)]
        chosen = matched if matched else rows[:_MAX_FACTS]
    else:
        chosen = rows[:_MAX_FACTS]

    facts: list[ExtractedValue] = []
    for ri, op in chosen[:_MAX_FACTS]:
        ref = ri.submission_id or ri.id
        label = WORKFLOW_LABELS.get(op.workflow, op.workflow)
        summary = {
            "workflow": label,
            "ref": ref,
            "review_status": _status_value(ri.status),
            "details": json.dumps(op.payload or {}, default=str)[:_MAX_PAYLOAD_CHARS],
        }
        facts.append(
            ExtractedValue(
                name=f"{label}:{ref}",
                value=summary,
                citation=Citation(
                    document_kind=DocumentKind.OTHER,
                    filename=f"{label} — {ref}",
                    locator=ri.id,
                ),
            )
        )
    return facts


async def gather_overview(session: AsyncSession, tenant_id: str) -> dict:
    """Real counts + recent refs for the assistant page's greeting and
    suggested prompts — no fixed/example submission ids, ever."""
    rows = await _recent_rows(session, tenant_id)

    pending_counts: dict[str, int] = {}
    for ri, op in rows:
        if ri.status == ReviewStatus.PENDING:
            label = WORKFLOW_LABELS.get(op.workflow, op.workflow)
            pending_counts[label] = pending_counts.get(label, 0) + 1

    recent = [
        {"workflow": WORKFLOW_LABELS.get(op.workflow, op.workflow), "ref": ri.submission_id or ri.id}
        for ri, op in rows[:8]
    ]
    return {"pending_counts": pending_counts, "recent": recent}
