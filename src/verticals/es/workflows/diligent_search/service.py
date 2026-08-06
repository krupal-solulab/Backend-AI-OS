"""Implements ``WorkflowPipeline[OutputPackage]`` for E&S Diligent Search &
Compliance Documentation.

The highest legal-stakes workflow in the vertical (PRD §8): a wrongly
generated affidavit is a potentially fraudulent record, not just a bad
recommendation. DS-04's document-generation gate is enforced in
``compliance_engine.determine_state`` (``document_eligible`` is only True
on a confirmed SUFFICIENT determination) — ``draft()`` below never
attempts to generate document text for any other state, so there is no
"partial/best-effort" path to bypass.

Two input shapes (detected from the scenario data itself): a single-state
case (``state`` + ``state_requirement`` + ``declinations_on_file`` at the
top level — Scenarios 01-03) or a multi-state case (``states`` + a
``state_requirements`` dict keyed by state code, which may omit entries
entirely — Scenario 04). Neither shape carries any raw document to
extract; the state reference data arrives already-structured, same
precedent as Renewal Remarketing.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from core.common.dtos import (
    Ctx,
    Decision,
    Draft,
    ExtractedModel,
    ExtractedValue,
    OutputPackage,
    RawBundle,
    RuleResult,
    WorkflowInput,
)
from core.common.enums import DecisionOutcome
from core.llm.service import LLMService
from verticals.es.workflows.diligent_search.compliance_engine import (
    StateDetermination,
    compute_overall_status,
    determine_state,
)
from verticals.es.workflows.diligent_search.scenario_loader import load_scenario
from verticals.es.workflows.diligent_search.schema import (
    ComplianceRecordPayload,
    DeclinationOut,
    StateDeterminationOut,
)

WORKFLOW_NAME = "diligent_search"
DEFAULT_WORKFLOW_N = 17  # Workflow_17 in TEST_DATA_ROOT — see DATA_AND_FIXTURES.md


class DiligentSearchPipeline:
    """One compliance-determination pass. See module docstring."""

    def __init__(self, llm: LLMService, *, workflow_n: int = DEFAULT_WORKFLOW_N) -> None:
        self._llm = llm
        self._workflow_n = workflow_n
        self._submission_id: str | None = None
        self._named_insured: str | None = None
        self._determinations: list[StateDetermination] = []
        self._overall_status: str = "COMPLETE"
        self._document_texts: dict[str, str] = {}

    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle:
        scenario_ref = inp.source_ref or inp.submission_id
        if not scenario_ref:
            raise ValueError("WorkflowInput needs source_ref or submission_id (a scenario ref)")
        context = load_scenario(self._workflow_n, scenario_ref)
        self._submission_id = context.get("submission_id")
        self._named_insured = context.get("named_insured")

        if "states" in context:
            requirements: dict[str, Any] = context.get("state_requirements", {})
            states_to_check: list[tuple[str, dict[str, Any] | None, list[dict[str, Any]] | None]]
            states_to_check = [
                (state, requirements.get(state), None) for state in context["states"]
            ]
        else:
            states_to_check = [
                (
                    context["state"],
                    context.get("state_requirement"),
                    context.get("declinations_on_file"),
                )
            ]

        self._determinations = [
            determine_state(state, requirement, declinations)
            for state, requirement, declinations in states_to_check
        ]
        self._overall_status = compute_overall_status(self._determinations)
        return RawBundle(submission_id=self._submission_id)

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        """No document extraction here — every scenario is already a
        structured JSON snapshot, same precedent as Renewal Remarketing."""
        fields = [
            ExtractedValue(name="named_insured", value=self._named_insured),
            ExtractedValue(name="overall_status", value=self._overall_status),
        ]
        return ExtractedModel(submission_id=raw.submission_id, fields=fields)

    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]:
        """DS-01..DS-05 are compound per-state decision logic, native in
        compliance_engine.py (Option-A) — nothing fits the generic 6-check
        rules engine."""
        return []

    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision:
        """BLOCKED/PARTIAL both mean "more determination or evidence is
        needed before this can proceed" — neither is a risk decline, so
        both map to REQUEST_INFO, same precedent as Package Assembly's
        BLOCKED->REQUEST_INFO mapping. The fine-grained distinction lives
        in the payload's own ``overall_status``, not lost."""
        outcome = (
            DecisionOutcome.PROCEED
            if self._overall_status == "COMPLETE"
            else DecisionOutcome.REQUEST_INFO
        )
        blocked = [s for s in self._determinations if s.sufficiency_status == "INSUFFICIENT"]
        pending = [
            s
            for s in self._determinations
            if s.requirement_status == "PENDING_DETERMINATION"
            or (s.requirement_status == "REQUIRED" and s.sufficiency_status == "NOT_APPLICABLE")
        ]
        if blocked:
            rationale = (
                f"Diligent search BLOCKED for {', '.join(s.state for s in blocked)}: "
                f"{'; '.join(s.gap_detail or '' for s in blocked)}"
            )
        elif pending:
            rationale = (
                f"Diligent search determination PARTIAL — pending for "
                f"{', '.join(s.state for s in pending)}."
            )
        else:
            rationale = "All states resolved: exempt or evidenced with a compliant document."
        return Decision(outcome=outcome, rationale=rationale)

    async def draft(self, ctx: Ctx, decision: Decision) -> Draft:
        """Generates the overall broker-facing summary AND, separately,
        one grounded document per DS-04-eligible state (stored in
        ``self._document_texts``, attached in ``package()``). Document
        generation NEVER runs for a non-eligible state — the gate is
        enforced upstream in ``compliance_engine.determine_state``, not
        here, so there's nothing to bypass by mistake."""
        for det in self._determinations:
            if not det.document_eligible:
                continue
            facts = [
                ExtractedValue(name="named_insured", value=self._named_insured),
                ExtractedValue(name="state", value=det.state),
                ExtractedValue(
                    name="declinations_on_file",
                    value=[
                        {
                            "carrier": d.carrier,
                            "date": d.date,
                            "written_evidence": d.written_evidence,
                        }
                        for d in det.declinations_on_file
                    ],
                ),
            ]
            prompt = (
                "Draft a diligent-search affidavit for this state, listing ONLY the admitted "
                "carriers, dates, and written-evidence confirmation given in the facts below — "
                "never invent, infer, or add a carrier, date, or evidence type not explicitly "
                "listed. This document may be relied on in a regulatory audit."
            )
            doc_draft = await self._llm.draft(ctx, prompt, facts, tier="standard")
            self._document_texts[det.state] = doc_draft.text

        summary_facts = [
            ExtractedValue(name="named_insured", value=self._named_insured),
            ExtractedValue(name="overall_status", value=self._overall_status),
            ExtractedValue(
                name="state_summaries",
                value=[
                    {
                        "state": s.state,
                        "requirement_status": s.requirement_status,
                        "sufficiency_status": s.sufficiency_status,
                        "gap_detail": s.gap_detail,
                    }
                    for s in self._determinations
                ],
            ),
        ]
        summary_prompt = (
            "Summarize this diligent-search compliance determination for a wholesale broker. "
            "Present it as a per-state checklist, never a single collapsed verdict — state "
            "plainly which states are resolved (exempt or compliant) and which still need "
            "action, grounded only in the facts given."
        )
        return await self._llm.draft(ctx, summary_prompt, summary_facts, tier="standard")

    async def package(self, ctx: Ctx, *args: object) -> OutputPackage:
        data, decision, draft = args[0], args[1], args[2]
        assert isinstance(data, ExtractedModel)
        assert isinstance(decision, Decision)
        assert isinstance(draft, Draft)

        state_outs = [
            StateDeterminationOut(
                state=det.state,
                requirement_status=det.requirement_status,
                exemption_basis=det.exemption_basis,
                declinations_required=det.declinations_required,
                declinations_on_file=[
                    DeclinationOut(
                        carrier=d.carrier, date=d.date, written_evidence=d.written_evidence
                    )
                    for d in det.declinations_on_file
                ],
                sufficiency_status=det.sufficiency_status,
                gap_detail=det.gap_detail,
                document_generated=det.document_eligible,
                generated_document_text=self._document_texts.get(det.state),
                retention_period_years=None,  # FR-8: not yet sourced, never guessed
            )
            for det in self._determinations
        ]
        payload = ComplianceRecordPayload(
            compliance_record_id=str(uuid4()),
            submission_id=self._submission_id,
            named_insured=self._named_insured,
            state_determinations=state_outs,
            overall_status=self._overall_status,
        )
        flags = [f"state_{s.state}_{s.requirement_status.lower()}" for s in self._determinations
                 if s.requirement_status != "REQUIRED" or s.sufficiency_status != "SUFFICIENT"]

        return OutputPackage(
            submission_id=self._submission_id,
            decision=decision,
            draft=draft,
            flags=flags,
            missing_info=[],
            citations=draft.citations,
            payload=payload.model_dump(),
        )

    async def run(self, ctx: Ctx, inp: WorkflowInput) -> OutputPackage:
        raw = await self.ingest(ctx, inp)
        data = await self.extract(ctx, raw)
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)

    async def run_live(self, ctx: Ctx, context: dict[str, Any]) -> OutputPackage:
        """Additive entry point, alongside ``run()``'s fixture-scenario path
        above — a real determination built from broker/compliance-supplied
        per-state facts and declination records for a real, MM-07-seeded
        submission, instead of a Workflow_17 fixture. ``context`` is shaped
        ``{submission_id, named_insured, states: [{state, requirement,
        declinations}]}`` — same per-state dict shape ``determine_state``
        already takes from the fixture path (see ``ingest()`` above), just
        sourced from a human instead of ``scenario_loader``. DS-01..DS-04's
        strict, unmodified logic decides sufficiency/generation exactly as
        it does today — this method supplies inputs, never a shortcut
        around the gate."""
        self._submission_id = context.get("submission_id")
        self._named_insured = context.get("named_insured")
        self._determinations = [
            determine_state(s["state"], s.get("requirement"), s.get("declinations"))
            for s in context.get("states", [])
        ]
        self._overall_status = compute_overall_status(self._determinations)

        raw = RawBundle(submission_id=self._submission_id)
        data = await self.extract(ctx, raw)
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)
