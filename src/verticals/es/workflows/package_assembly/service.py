"""Implements ``WorkflowPipeline[OutputPackage]`` for E&S Package Assembly.

Unlike Market Matching, this workflow doesn't ingest raw documents at all —
its input is the PREVIOUS workflow's decision output
(``market_matching_output.json`` / a real Decision.details["matches"] entry
in production). ``ingest()``/``extract()`` are consequently a looser fit than
usual for this workflow (see each method's docstring for how they're
mapped); ``decide()``/``package()`` carry the real logic, per
docs/WORKFLOW_TEMPLATE.md's usual pattern.

One instance per (submission, carrier) assembly pass — state resolved by
``ingest()`` is read by every later step, so a fresh instance must be built
per carrier in a multi-carrier selection (see router.py).
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
    ExtractedValue,
    OutputPackage,
    RawBundle,
    RuleResult,
    WorkflowInput,
)
from core.common.enums import DecisionOutcome
from core.extraction.service import ExtractionService
from core.llm.service import LLMService
from verticals.es.workflows.package_assembly.assembly import PackageResult, assemble_package
from verticals.es.workflows.package_assembly.live_ingestion import (
    build_live_carrier_view,
    build_live_extracted_model,
    live_completeness_check,
    real_market_matching_payload,
)
from verticals.es.workflows.package_assembly.scenario_loader import carrier_view, load_scenario
from verticals.es.workflows.package_assembly.schema import (
    BlockingItemOut,
    CoverLetterCitationOut,
    CoverLetterOut,
    DocChecklistItemOut,
    GapItemOut,
    PackageAssemblyPayload,
    StatusLogEntryOut,
    SupplementalFieldOut,
)
from verticals.es.workflows.package_assembly.submission_resolver import (
    MARKET_MATCHING_WORKFLOW_N,
    resolve_extracted_model,
)

WORKFLOW_NAME = "package_assembly"
DEFAULT_WORKFLOW_N = 11  # Workflow_11 in TEST_DATA_ROOT — see DATA_AND_FIXTURES.md

_OUTCOME_BY_STATUS = {
    "READY": DecisionOutcome.PROCEED,
    "READY_WITH_GAP": DecisionOutcome.PROCEED,
    "BLOCKED": DecisionOutcome.REQUEST_INFO,
}


class PackageAssemblyPipeline:
    """One (submission, carrier) assembly pass. See module docstring."""

    def __init__(
        self,
        extraction: ExtractionService,
        llm: LLMService,
        *,
        workflow_n: int = DEFAULT_WORKFLOW_N,
        market_matching_workflow_n: int = MARKET_MATCHING_WORKFLOW_N,
    ) -> None:
        self._extraction = extraction
        self._llm = llm
        self._workflow_n = workflow_n
        self._mm_workflow_n = market_matching_workflow_n
        self._carrier_view: dict[str, Any] | None = None
        self._result: PackageResult | None = None
        self._is_live = False

    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle:
        """Loads the scenario's market_matching_output.json + resolves the
        one carrier this pass assembles for. There's no raw document
        ingestion here (see module docstring) — RawBundle just carries the
        submission id through the pipeline's shape."""
        scenario_ref = inp.source_ref or inp.submission_id
        if not scenario_ref:
            raise ValueError("WorkflowInput needs source_ref or submission_id (a scenario ref)")
        carrier_id = (inp.params or {}).get("carrier_id")

        scenario = load_scenario(self._workflow_n, scenario_ref)
        self._carrier_view = carrier_view(scenario, carrier_id)
        return RawBundle(submission_id=self._carrier_view.get("submission_id"))

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        """Re-derives the underlying submission's ExtractedModel by matching
        named_insured against Workflow_10 — see submission_resolver.py's
        docstring for why this is necessary and why it's safe."""
        assert self._carrier_view is not None, "ingest() must run before extract()"
        named_insured = self._carrier_view.get("named_insured")
        if not named_insured:
            return ExtractedModel(submission_id=raw.submission_id, fields=[])
        return await resolve_extracted_model(
            ctx, named_insured, self._extraction, workflow_n=self._mm_workflow_n
        )

    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]:
        """This workflow doesn't route anything through core.rules_engine —
        every PA-01..PA-07 check is compound/cross-field logic that lives
        natively in assembly.py (Option-A pattern). Nothing to validate here."""
        return []

    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision:
        assert self._carrier_view is not None, "ingest() must run before decide()"
        result = (
            assemble_package(self._carrier_view, data, document_check_fn=live_completeness_check)
            if self._is_live
            else assemble_package(self._carrier_view, data)
        )
        self._result = result

        blocking_summary = "; ".join(f"{b.item} ({b.reason})" for b in result.blocking_items)
        gap_summary = "; ".join(g.item for g in result.gap_items_disclosed)
        rationale = f"Package status: {result.status}"
        if blocking_summary:
            rationale += f" — blocked by: {blocking_summary}"
        elif gap_summary:
            rationale += f" — disclosed gap: {gap_summary}"

        return Decision(
            outcome=_OUTCOME_BY_STATUS[result.status],
            rationale=rationale,
            details={
                "package_status": result.status,
                "blocking_items": [b.item for b in result.blocking_items],
                "gap_items": [g.item for g in result.gap_items_disclosed],
            },
        )

    async def draft(self, ctx: Ctx, decision: Decision) -> Draft:
        """Carrier-tailored cover letter (PA-04): grounded facts include the
        carrier's own appetite notes (never a generic template), any inlined
        loss-history summary, and — critically — only THIS carrier's own
        gap/blocking items (per Scenario 03: the same loss-run fact must not
        appear in a carrier's letter unless it's actually a gap FOR THAT
        CARRIER's own stated requirement)."""
        assert self._carrier_view is not None, "ingest() must run before draft()"
        assert self._result is not None, "decide() must run before draft()"
        view = self._carrier_view
        result = self._result
        facts: list[ExtractedValue] = []

        named_insured = view.get("named_insured")
        if named_insured:
            facts.append(ExtractedValue(name="submission.named_insured", value=named_insured))
        if view.get("carrier_notes"):
            facts.append(ExtractedValue(name="carrier.notes", value=view["carrier_notes"]))
        for key, value in (view.get("loss_history_summary") or {}).items():
            facts.append(ExtractedValue(name=f"loss_history.{key}", value=value))
        for gap in result.gap_items_disclosed:
            if gap.cover_letter_acknowledgment:
                facts.append(ExtractedValue(name="package.gap_item", value=gap.item))
        for blocking in result.blocking_items:
            value = f"{blocking.item}: {blocking.reason}"
            facts.append(ExtractedValue(name="package.blocking_item", value=value))

        prompt = (
            "Draft a cover letter for this carrier submission package. "
            "Reference this carrier's own appetite/notes where available — never "
            "a generic template. Proactively disclose any risk signals or gap "
            "items listed in the facts; do not omit or minimize them."
        )
        return await self._llm.draft(ctx, prompt, facts, tier="standard")

    async def package(self, ctx: Ctx, *args: object) -> OutputPackage:
        data, decision, draft = args[0], args[1], args[2]
        assert isinstance(data, ExtractedModel)
        assert isinstance(decision, Decision)
        assert isinstance(draft, Draft)
        assert self._carrier_view is not None and self._result is not None

        view = self._carrier_view
        result = self._result

        payload = PackageAssemblyPayload(
            package_id=str(uuid4()),
            submission_id=view.get("submission_id"),
            carrier_id=view["carrier_id"],
            carrier_name=view.get("carrier_name", view["carrier_id"]),
            status=result.status,
            document_checklist=[
                DocChecklistItemOut(
                    document_type=d.document_type, included=d.included, source=d.source
                )
                for d in result.document_checklist
            ],
            supplemental_form_fields=[
                SupplementalFieldOut(
                    field_name=f.field_name,
                    value=f.value,
                    auto_filled=f.auto_filled,
                    source_citation=f.source_citation,
                )
                for f in result.supplemental_form_fields
            ],
            diligent_search_attached=result.diligent_search_attached,
            cover_letter=CoverLetterOut(
                body=draft.text,
                citations=[
                    CoverLetterCitationOut(claim=c.locator or c.filename, source=c.filename)
                    for c in draft.citations
                ],
            ),
            blocking_items=[
                BlockingItemOut(item=b.item, reason=b.reason) for b in result.blocking_items
            ],
            gap_items_disclosed=[GapItemOut(item=g.item) for g in result.gap_items_disclosed],
            status_log=[
                StatusLogEntryOut(
                    action="generated", timestamp=datetime.now(UTC).isoformat(), user=ctx.user_id
                )
            ],
        )

        missing_info = [b.item for b in result.blocking_items] + [
            g.item for g in result.gap_items_disclosed
        ]
        flags = [f"BLOCKED: {b.item} — {b.reason}" for b in result.blocking_items]

        return OutputPackage(
            submission_id=view.get("submission_id"),
            decision=decision,
            draft=draft,
            flags=flags,
            missing_info=missing_info,
            citations=draft.citations,
            payload=payload.model_dump(),
        )

    async def run(self, ctx: Ctx, inp: WorkflowInput) -> OutputPackage:
        """Convenience orchestration of the full pipeline for ONE carrier.
        For a multi-carrier selection, the caller (router.py) builds one
        fresh PackageAssemblyPipeline instance per carrier and calls this
        once each — never reuses one instance across carriers, since
        per-run state is stashed on `self`."""
        raw = await self.ingest(ctx, inp)
        data = await self.extract(ctx, raw)
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)

    async def run_live(
        self, ctx: Ctx, session: AsyncSession, market_matching_review_item_id: str, carrier_id: str
    ) -> OutputPackage:
        """Additive entry point, alongside ``run()``'s fixture-scenario path
        above — assembles a real package for one carrier from an ACTUAL
        Market Matching review item instead of a Workflow_11 fixture. See
        ``live_ingestion.py`` for the real data sources and the coarser,
        type-only document completeness check this path deliberately uses.

        Deliberately does NOT call ``self.extract()`` — that method's
        ``resolve_extracted_model()`` scans Workflow_10 FIXTURES for a
        matching named insured (see its docstring) and would raise on any
        real name. ``build_live_extracted_model()`` re-derives the real
        ExtractedModel from this submission's actual persisted documents
        instead."""
        self._is_live = True
        payload = await real_market_matching_payload(
            session, ctx, market_matching_review_item_id
        )
        submission_id = payload.get("submission_id")
        data = await build_live_extracted_model(session, ctx, submission_id)
        self._carrier_view = await build_live_carrier_view(
            session, ctx, market_matching_review_item_id, carrier_id, extracted_model=data
        )
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)
