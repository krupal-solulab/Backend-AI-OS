"""Cross-workflow hook: fires a real, linked Diligent Search review item
stub from Market Matching's own zero-match result (MM-07), per the approved
Phase 2 connectivity plan — same "reach across workflow boundaries"
precedent as ``agent_communication_hooks.py``, kept to its own file for the
same reason (this cross-cutting wiring stays visible and contained, and
``market_matching``'s own router gains only a single extra call).

Same **no-throw boundary** convention as ``agent_communication_hooks.py``:
this must never surface an exception to, or change the response of, Market
Matching's own ``/run`` — a failure here is logged and swallowed.

**Deliberately a STUB, not a full auto-determination.** ``MarketMatchingPayload``'s
``diligent_search`` block only carries a count/compliance flag — it has no
per-state, per-carrier declination history, which is what
``DiligentSearchPipeline`` actually needs to reach a real per-state
determination (see ``diligent_search/service.py``). Auto-running that
pipeline off this flag would mean fabricating declination records that
don't exist. Instead, this creates a real ``ReviewItem`` (with a minimal,
honestly-empty ``ComplianceRecordPayload`` — zero states determined yet) so
the submission is visible in the Diligent Search queue for a human to
complete the actual search, rather than faking a determination.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx, Decision, OutputPackage
from core.common.enums import DecisionOutcome
from core.models import ReviewItem as ReviewItemRow
from core.review_queue import DefaultReviewQueueService

log = logging.getLogger(__name__)

WORKFLOW_NAME = "diligent_search"


async def _already_seeded(session: AsyncSession, ctx: Ctx, submission_id: str | None) -> bool:
    if not submission_id:
        return False
    existing = (
        await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.workflow) == WORKFLOW_NAME,
                col(ReviewItemRow.submission_id) == submission_id,
            )
        )
    ).scalars().first()
    return existing is not None


async def fire_diligent_search_required(
    session: AsyncSession, ctx: Ctx, market_matching_output: OutputPackage
) -> None:
    """Market Matching zero-match + ``diligent_search.required`` -> a real,
    linked Diligent Search review item stub, per MM-07. A no-op if one
    already exists for this submission (never duplicates on a re-run)."""
    try:
        payload = market_matching_output.payload or {}
        diligent_search = payload.get("diligent_search") or {}
        if not diligent_search.get("required"):
            return
        if await _already_seeded(session, ctx, market_matching_output.submission_id):
            return

        stub_payload = {
            "compliance_record_id": str(uuid4()),
            "submission_id": market_matching_output.submission_id,
            # not carried on MarketMatchingPayload (see agent_communication_hooks.py's
            # documented v1 limitation on named_insured) — honestly absent, not guessed.
            "named_insured": None,
            "state_determinations": [],
            "overall_status": "BLOCKED",
        }
        output = OutputPackage(
            submission_id=market_matching_output.submission_id,
            decision=Decision(
                outcome=DecisionOutcome.REQUEST_INFO,
                rationale=(
                    "Seeded from Market Matching's zero-carrier-match result (MM-07) — no "
                    "per-state declination search has been performed yet; a human must "
                    "complete it before any determination is made."
                ),
            ),
            draft=None,
            flags=["seeded_from_market_matching"],
            missing_info=["per-state diligent search declination evidence"],
            payload=stub_payload,
        )
        await DefaultReviewQueueService().enqueue(session, ctx, output, WORKFLOW_NAME)
    except Exception:
        log.exception(
            "diligent_search auto-seed failed for submission %s — market_matching's own "
            "response is unaffected",
            market_matching_output.submission_id,
        )
