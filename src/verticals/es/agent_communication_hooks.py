"""Cross-workflow hook: fires Agent Communication drafts automatically from
Market Matching's, Package Assembly's, Quote Comparison's, and Binder &
Policy Issuance's own successful calls, per
``Data sets/Workflow 3/PRD_Retail_Agent_Communication_Copilot.md`` §4/§5.2.

Binder & Issuance's ``fire_binder_issuance_result`` introduces the 7th
trigger type, ``POLICY_DOCUMENTS_DELIVERED`` (per that workflow's PRD
FR-19 — a coordinated extension to this PRD, not something Binder &
Issuance's own PRD could specify unilaterally). Both
``PLACEMENT_CONFIRMATION`` and ``POLICY_DOCUMENTS_DELIVERED`` fire
automatically off Binder & Issuance's own run/resolve calls (same pattern
as Market Matching/Package Assembly, NOT Quote Comparison's
select-is-the-trigger pattern) — BI-06's gate is "only on a verified-clean
reconciliation," already computed by that workflow's own
``downstream_triggers_fired`` flags before this hook is ever called.

Quote Comparison's ``fire_quote_comparison_result`` is the one exception to
"fires right after the upstream workflow's own /run": per that workflow's
PRD (FR-20/FR-23 — "broker marks which quote(s) to present... which feeds
FR-20's downstream trigger"), the broker's own selection is the trigger, not
Quote Comparison's recommendation output by itself. So this one is called
from quote_comparison's ``/{item_id}/select/{quote_id}`` action, not its
``/run`` — see that router's docstring.

This deliberately reaches across workflow boundaries — WORKFLOW_TEMPLATE.md's
usual per-workflow-folder ownership rule bends here because this IS the
cross-workflow wiring the PRD calls for. Kept to this one file (rather than
inlined in each workflow) so that reach stays visible and contained, and
``market_matching``/``package_assembly``'s own routers gain only a single
call each — see their ``run_*`` handlers.

**No-throw boundary, always.** Every public function here swallows and logs
its own exceptions. A drafting failure must never surface to, or change the
response of, the workflow that triggered it — Market Matching/Package
Assembly's callers must see byte-identical behavior whether this hook
succeeds, fails, or (in dev, no ``TEST_DATA_ROOT``/LLM key) does either.

**Known v1 limitation, accepted deliberately (see STATUS.md):**
``MarketMatchingPayload``/``PackageAssemblyPayload`` don't carry
``named_insured`` — both pipelines resolve ``acord.named_insured`` internally
(see ``package_assembly/submission_resolver.py``) but never return it past
their own ``pipeline.run()``. Rather than add new fields to either payload
("only from data these workflows already produce, no new extraction"),
auto-fired drafts simply lack it; ``drafting.py``'s ``_subject_line``/
``build_facts`` already treat every trigger field as optional and fall back
gracefully (a generic "Submission - ..." subject line), so this degrades
quality, not correctness.

**Scope decision, per the approved plan (not a mechanical default):**
Submission Acknowledgment fires **per carrier** from Package Assembly's own
run (one ack per carrier, using that carrier's own status) rather than one
draft combining every carrier on the submission — the combined version the
PRD's own Trigger 01 sample shows needs tracking "how many carriers were
selected" and waiting for all of them to be packaged, which is real new
infrastructure beyond this pass's thin-hook scope, not a mechanical
extension of it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import Ctx, OutputPackage, WorkflowInput
from core.common.enums import DecisionOutcome
from core.llm import build_llm_service
from core.review_queue import DefaultReviewQueueService
from verticals.es.workflows.agent_communication.router import (
    _find_pending_duplicate,
)
from verticals.es.workflows.agent_communication.service import (
    WORKFLOW_NAME,
    AgentCommunicationPipeline,
)

log = logging.getLogger(__name__)


async def _draft_and_enqueue(
    session: AsyncSession, ctx: Ctx, submission_id: str | None, trigger: dict[str, Any]
) -> None:
    """Shared draft-and-enqueue path. Reuses ``agent_communication.router``'s
    own FR-5 dedup helper (calling it, not reimplementing it) so an auto-fire
    never creates a second draft alongside an existing unresolved one — the
    same rule a manual ``POST /run`` gets."""
    trigger_type = trigger.get("trigger_type")
    carrier_name = trigger.get("carrier_name")
    dup = await _find_pending_duplicate(session, ctx, submission_id, trigger_type, carrier_name)
    if dup is not None:
        return
    pipeline = AgentCommunicationPipeline(session=session, llm=build_llm_service())
    inp = WorkflowInput(submission_id=submission_id, params={"trigger_data": trigger})
    output = await pipeline.run(ctx, inp)
    await DefaultReviewQueueService().enqueue(session, ctx, output, WORKFLOW_NAME)


async def fire_no_market_found(session: AsyncSession, ctx: Ctx, output: OutputPackage) -> None:
    """Market Matching zero-match -> NO_MARKET_FOUND (PRD §5.2 row 3).
    ``Decision.outcome is DECLINE`` is the precise zero-match signal — distinct
    from ``REQUEST_INFO`` (missing ACORD), which is not a true zero-match and
    must not auto-fire this trigger."""
    try:
        if output.decision.outcome is not DecisionOutcome.DECLINE:
            return
        payload = output.payload or {}
        matches = payload.get("matches") or []
        excluded = payload.get("excluded") or []
        trigger = {
            "trigger_type": "NO_MARKET_FOUND",
            "source_workflow": "Market Matching",
            "submission_id": output.submission_id,
            "zero_match_result": True,
            "carriers_reviewed_count": len(matches) + len(excluded),
            "diligent_search": payload.get("diligent_search"),
        }
        await _draft_and_enqueue(session, ctx, output.submission_id, trigger)
    except Exception:
        log.exception(
            "agent_communication auto-fire (NO_MARKET_FOUND) failed for submission %s — "
            "market_matching's own response is unaffected",
            output.submission_id,
        )


async def fire_package_assembly_result(
    session: AsyncSession, ctx: Ctx, output: OutputPackage
) -> None:
    """Package Assembly result -> SUBMISSION_ACKNOWLEDGMENT (READY) or
    MISSING_INFO_REQUEST (BLOCKED / READY_WITH_GAP) — PRD §5.2 rows 1/2, one
    per carrier per this pass's approved v1 scope (see module docstring)."""
    try:
        payload = output.payload or {}
        pkg_status = payload.get("status")
        carrier_name = payload.get("carrier_name")

        if pkg_status == "READY":
            trigger: dict[str, Any] = {
                "trigger_type": "SUBMISSION_ACKNOWLEDGMENT",
                "source_workflow": "Package Assembly",
                "submission_id": output.submission_id,
                "carriers_approached": [carrier_name] if carrier_name else [],
                "carrier_statuses": (
                    {carrier_name: "READY - submitted"} if carrier_name else {}
                ),
            }
        elif pkg_status in ("BLOCKED", "READY_WITH_GAP"):
            items = (
                payload.get("blocking_items")
                if pkg_status == "BLOCKED"
                else payload.get("gap_items_disclosed")
            )
            trigger = {
                "trigger_type": "MISSING_INFO_REQUEST",
                "source_workflow": "Package Assembly",
                "submission_id": output.submission_id,
                "carrier_name": carrier_name,
                "package_status": pkg_status,
                "blocking_items": [i.get("item") for i in (items or []) if i.get("item")],
            }
        else:
            return

        await _draft_and_enqueue(session, ctx, output.submission_id, trigger)
    except Exception:
        log.exception(
            "agent_communication auto-fire failed for submission %s — "
            "package_assembly's own response is unaffected",
            output.submission_id,
        )


async def fire_quote_comparison_result(
    session: AsyncSession,
    ctx: Ctx,
    *,
    submission_id: str | None,
    named_insured: str | None,
    selected_quote: dict[str, Any],
) -> None:
    """Broker-selected quote -> QUOTE_TERMS_SUMMARY (PRD §5.2 row 4),
    replacing that trigger's v1 manual-input fallback (Quote Comparison
    PRD's FR-20). Called from quote_comparison's own ``/select`` action, not
    its ``/run`` — see module docstring. A declination can't be selected as
    a "quote" to summarize; that path is a no-op, not an error."""
    try:
        if selected_quote.get("response_type") != "QUOTE":
            return
        material_context = "; ".join(
            s["description"]
            for s in (selected_quote.get("subjectivities") or [])
            if s.get("materiality") == "material"
        )
        trigger = {
            "trigger_type": "QUOTE_TERMS_SUMMARY",
            "source_workflow": "Quote Comparison",
            "submission_id": submission_id,
            "named_insured": named_insured,
            "carrier_name": selected_quote.get("carrier_name"),
            "quoted_terms": {
                "premium": selected_quote.get("premium"),
                "limits": selected_quote.get("limits"),
            },
            "quote_context": material_context or None,
        }
        await _draft_and_enqueue(session, ctx, submission_id, trigger)
    except Exception:
        log.exception(
            "agent_communication auto-fire (QUOTE_TERMS_SUMMARY) failed for submission %s — "
            "quote_comparison's own response is unaffected",
            submission_id,
        )


async def fire_binder_issuance_result(
    session: AsyncSession, ctx: Ctx, *, submission_id: str | None, payload: dict[str, Any]
) -> None:
    """Fires PLACEMENT_CONFIRMATION and/or POLICY_DOCUMENTS_DELIVERED per
    BI-06's gate — ``payload["downstream_triggers_fired"]`` is already
    computed by binder_issuance's own service (true only on a verified-clean
    reconciliation, per FR-18/FR-19); this function just builds and enqueues
    whichever triggers are eligible, grounded in the VERIFIED/RECONCILED
    terms (FR-20), never the originally requested ones."""
    triggers_fired = payload.get("downstream_triggers_fired") or {}
    named_insured = payload.get("named_insured")
    carrier_name = payload.get("carrier_name")

    if triggers_fired.get("placement_confirmation"):
        try:
            confirmed = (payload.get("carrier_confirmation") or {}).get("confirmed_terms") or {}
            trigger = {
                "trigger_type": "PLACEMENT_CONFIRMATION",
                "source_workflow": "Binder & Policy Issuance Coordination",
                "submission_id": submission_id,
                "named_insured": named_insured,
                "carrier_name": carrier_name,
                "bound_terms": confirmed,
            }
            await _draft_and_enqueue(session, ctx, submission_id, trigger)
        except Exception:
            log.exception(
                "agent_communication auto-fire (PLACEMENT_CONFIRMATION) failed for "
                "submission %s — binder_issuance's own response is unaffected",
                submission_id,
            )

    if triggers_fired.get("policy_documents_delivered"):
        try:
            trigger = {
                "trigger_type": "POLICY_DOCUMENTS_DELIVERED",
                "source_workflow": "Binder & Policy Issuance Coordination",
                "submission_id": submission_id,
                "named_insured": named_insured,
                "carrier_name": carrier_name,
                "binder_number": (payload.get("carrier_confirmation") or {}).get("binder_number"),
                "verified_terms": (
                    payload.get("carrier_confirmation") or {}
                ).get("confirmed_terms"),
            }
            await _draft_and_enqueue(session, ctx, submission_id, trigger)
        except Exception:
            log.exception(
                "agent_communication auto-fire (POLICY_DOCUMENTS_DELIVERED) failed for "
                "submission %s — binder_issuance's own response is unaffected",
                submission_id,
            )


async def fire_endorsement_result(
    session: AsyncSession, ctx: Ctx, *, submission_id: str | None, payload: dict[str, Any]
) -> None:
    """Fires the 8th trigger type, ENDORSEMENT_CONFIRMED, per Endorsement /
    Mid-Term Change Processing's FR-16 — only once EP-05's issued-endorsement
    reconciliation is verified clean or broker-resolved
    (``payload["downstream_trigger_fired"]``, already computed by
    endorsement's own service/router before this is ever called). Fires from
    that workflow's own ``/run``/``resolve-discrepancy`` actions, same
    pattern as Binder & Issuance — not a broker "select" step."""
    if not payload.get("downstream_trigger_fired"):
        return
    try:
        carrier_response = payload.get("carrier_response") or {}
        requested_change = payload.get("requested_change") or {}
        trigger = {
            "trigger_type": "ENDORSEMENT_CONFIRMED",
            "source_workflow": "Endorsement / Mid-Term Change Processing",
            "submission_id": submission_id,
            "named_insured": payload.get("named_insured"),
            "carrier_name": payload.get("carrier_name"),
            "endorsement_number": carrier_response.get("endorsement_number"),
            "requested_change_detail": requested_change.get("detail"),
            "issued_items": carrier_response.get("issued_items"),
        }
        await _draft_and_enqueue(session, ctx, submission_id, trigger)
    except Exception:
        log.exception(
            "agent_communication auto-fire (ENDORSEMENT_CONFIRMED) failed for "
            "submission %s — endorsement's own response is unaffected",
            submission_id,
        )
