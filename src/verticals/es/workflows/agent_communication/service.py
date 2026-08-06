"""Implements ``WorkflowPipeline[OutputPackage]`` for E&S Retail Agent
Communication.

Unlike the other two E&S workflows, this one does no fresh extraction or
document ingestion at all — its input is already the structured output of an
upstream workflow (Market Matching / Package Assembly) or a manually-logged
trigger (PRD §1/FR-2). ``ingest()``/``extract()`` are consequently a thin
pass-through (see each docstring); ``decide()`` carries the real logic —
deterministic trigger classification + RA-TN tone/framing selection + the
compliance-gate determination (native Option-A logic in ``drafting.py``,
PRD §8's "hard-rule-before-LLM" principle). ``draft()`` only asks the LLM to
write prose from already-decided facts + framing instructions; it never picks
the framing itself.

One instance per run — like Market Matching (not Package Assembly), this
pipeline takes an ``AsyncSession`` in its constructor, needed by ``decide()``
to resolve a NO_RESPONSE_FOLLOWUP draft's subject line against a prior draft
in this workflow's own history (FR-10; see ``subject_resolver.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import (
    Ctx,
    Decision,
    Draft,
    ExtractedModel,
    OutputPackage,
    RawBundle,
    RuleResult,
    WorkflowInput,
)
from core.common.enums import DecisionOutcome
from core.llm.service import LLMService
from verticals.es.workflows.agent_communication.drafting import (
    PreparedCommunication,
    classify_trigger_type,
    prepare,
    with_subject,
)
from verticals.es.workflows.agent_communication.schema import (
    DraftCommunicationOut,
    GroundingCitationOut,
)
from verticals.es.workflows.agent_communication.subject_resolver import resolve_followup_subject

WORKFLOW_NAME = "agent_communication"
DEFAULT_WORKFLOW_N = 12  # Workflow_12 in TEST_DATA_ROOT — see DATA_AND_FIXTURES.md


class AgentCommunicationPipeline:
    """One trigger -> one drafted communication. See module docstring."""

    def __init__(self, session: AsyncSession, llm: LLMService) -> None:
        self._session = session
        self._llm = llm
        self._trigger_data: dict[str, Any] | None = None
        self._prepared: PreparedCommunication | None = None

    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle:
        """Stashes the triggering object (already-structured data, not a raw
        document) — ``RawBundle`` just carries ``submission_id`` through so the
        pipeline shape holds."""
        data = inp.params.get("trigger_data")
        if not data:
            raise ValueError("WorkflowInput.params['trigger_data'] is required")
        self._trigger_data = data
        return RawBundle(submission_id=data.get("submission_id"))

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        """No re-extraction here — this workflow's input is already the fully
        structured output of an upstream workflow or a manual log entry, not
        raw documents to classify/extract from."""
        return ExtractedModel(submission_id=raw.submission_id, fields=[])

    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]:
        """Every check here is compound trigger/tone routing logic (Option-A,
        native in drafting.py) — nothing fits the generic 6-check rules
        engine."""
        return []

    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision:
        assert self._trigger_data is not None, "ingest() must run before decide()"
        trigger_data = self._trigger_data
        trigger_type = classify_trigger_type(trigger_data)
        prepared = prepare(trigger_type, trigger_data)

        if trigger_type == "NO_RESPONSE_FOLLOWUP":
            subject = await resolve_followup_subject(
                self._session,
                ctx,
                workflow=WORKFLOW_NAME,
                submission_id=trigger_data.get("submission_id"),
                original_request_type=trigger_data.get("original_request_type"),
                carrier_name=trigger_data.get("carrier_name"),
                named_insured=trigger_data.get("named_insured"),
            )
            prepared = with_subject(prepared, subject)

        self._prepared = prepared
        rationale = f"Draft prepared: {trigger_type}"
        if prepared.requires_compliance_review:
            rationale += " — held for compliance review (RA-TN-06)"
        return Decision(
            outcome=DecisionOutcome.PROCEED,
            rationale=rationale,
            details={
                "trigger_type": trigger_type,
                "carrier_name": prepared.carrier_name,
                "requires_compliance_review": prepared.requires_compliance_review,
            },
        )

    async def draft(self, ctx: Ctx, decision: Decision) -> Draft:
        assert self._prepared is not None, "decide() must run before draft()"
        return await self._llm.draft(
            ctx, self._prepared.tone_instructions, self._prepared.facts, tier="standard"
        )

    async def package(self, ctx: Ctx, *args: object) -> OutputPackage:
        data, decision, draft = args[0], args[1], args[2]
        assert isinstance(data, ExtractedModel)
        assert isinstance(decision, Decision)
        assert isinstance(draft, Draft)
        assert self._prepared is not None and self._trigger_data is not None
        prepared = self._prepared
        trigger_data = self._trigger_data

        payload = DraftCommunicationOut(
            draft_id=str(uuid4()),
            trigger_type=prepared.trigger_type,
            source_workflow=str(trigger_data.get("source_workflow") or ""),
            source_record_id=trigger_data.get("submission_id"),
            submission_id=trigger_data.get("submission_id"),
            named_insured=trigger_data.get("named_insured"),
            carrier_name=prepared.carrier_name,
            retail_agent_name=trigger_data.get("retail_agent_name"),
            retail_agency=trigger_data.get("retail_agency"),
            subject_line=prepared.subject_line,
            body=draft.text,
            requires_compliance_review=prepared.requires_compliance_review,
            carrier_names_disclosed=prepared.carrier_names_disclosed,
            grounding_citations=[
                GroundingCitationOut(claim=str(f.value)[:160], source_field=f.name)
                for f in prepared.facts
            ],
            status="UNDER_COMPLIANCE_REVIEW" if prepared.requires_compliance_review else "DRAFT",
            generated_timestamp=datetime.now(UTC).isoformat(),
        )

        flags = ["requires_compliance_review"] if prepared.requires_compliance_review else []

        return OutputPackage(
            submission_id=trigger_data.get("submission_id"),
            decision=decision,
            draft=draft,
            flags=flags,
            missing_info=[],
            citations=draft.citations,
            payload=payload.model_dump(),
        )

    async def run(self, ctx: Ctx, inp: WorkflowInput) -> OutputPackage:
        """Convenience orchestration of the full pipeline for one trigger."""
        raw = await self.ingest(ctx, inp)
        data = await self.extract(ctx, raw)
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)
