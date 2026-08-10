"""Implements ``WorkflowPipeline[OutputPackage]`` for E&S Market Matching — the
first E&S workflow (docs/WORKFLOW_TEMPLATE.md). Per that template, almost every
step just calls a shared service; the only real per-workflow code is
``decide()`` (which rule sets to run + how to map results — here, delegating
to ``verticals.es.decision_core``) and ``package()`` (this workflow's output
shape).
"""

from __future__ import annotations

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
from core.documents.store import DocumentStore
from core.extraction.service import ExtractionService
from core.ingestion.connectors import ConnectorService
from core.llm.service import LLMService
from core.rules_engine import DefaultRulesEngine
from verticals.es.decision_core import (
    SUBMISSION_VALIDATION_KEY,
    decide_market_match,
    load_carrier_panel,
    seed_and_publish_carrier_rulesets,
    seed_and_publish_submission_validation_ruleset,
)
from verticals.es.workflows.market_matching.schema import (
    CarrierMatchOut,
    DiligentSearchOut,
    ExcludedCarrierOut,
    MarketMatchingPayload,
)

WORKFLOW_NAME = "market_matching"
DEFAULT_WORKFLOW_N = 10  # Workflow_10 in TEST_DATA_ROOT — see DATA_AND_FIXTURES.md


class MarketMatchingPipeline:
    """The E&S Market Matching pipeline. One instance per request/task."""

    def __init__(
        self,
        session: AsyncSession,
        connector: ConnectorService,
        extraction: ExtractionService,
        rules_engine: DefaultRulesEngine,
        llm: LLMService,
        documents: DocumentStore | None = None,
        workflow_n: int = DEFAULT_WORKFLOW_N,
    ) -> None:
        self._session = session
        self._connector = connector
        self._extraction = extraction
        self._rules_engine = rules_engine
        self._llm = llm
        self._documents = documents
        self._workflow_n = workflow_n

    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle:
        message_id = inp.source_ref or inp.submission_id
        if not message_id:
            raise ValueError("WorkflowInput needs submission_id or source_ref to ingest")
        raw = await self._connector.to_raw_bundle(ctx, message_id)
        if self._documents is not None:
            submission_id = raw.submission_id or message_id
            for doc in raw.documents:
                await self._documents.save(self._session, ctx, submission_id, doc)
        return raw

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        return await self._extraction.extract(ctx, raw)

    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]:
        """Baseline submission-level validation only (is there even an ACORD to
        read a class code / premium from?) — per-carrier appetite/completeness
        checks run inside decide(), not here (see decision_core/matching.py)."""
        await seed_and_publish_submission_validation_ruleset(self._session, ctx)
        return await self._rules_engine.evaluate(
            self._session, ctx, SUBMISSION_VALIDATION_KEY, data
        )

    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision:
        validation_results = await self.validate(ctx, data)
        acord_missing = any(
            not r.passed for r in validation_results if r.rule_id == "doc.acord.required"
        )
        if acord_missing:
            return Decision(
                outcome=DecisionOutcome.REQUEST_INFO,
                rationale="Missing ACORD application — cannot determine class code or "
                "indicated premium, so carrier matching cannot run.",
                rule_results=validation_results,
            )

        panel = load_carrier_panel(self._workflow_n)
        await seed_and_publish_carrier_rulesets(self._session, ctx, panel)
        decision = await decide_market_match(self._session, ctx, self._rules_engine, panel, data)
        return decision.model_copy(update={"rule_results": validation_results})

    async def draft(self, ctx: Ctx, decision: Decision) -> Draft:
        matches = decision.details.get("matches", [])
        if not matches:
            return Draft(
                text="No carrier on the current panel fits this submission's appetite data. "
                f"{decision.details.get('diligent_search', {}).get('note', '')}",
                citations=[],
            )
        top = matches[0]
        facts = [
            ExtractedValue(name="top_match.carrier_name", value=top["carrier_name"]),
            ExtractedValue(name="top_match.score", value=top["score"]),
            ExtractedValue(name="top_match.missing", value=", ".join(top["missing"]) or "none"),
            ExtractedValue(name="panel_size", value=len(matches)),
        ]
        return await self._llm.draft(
            ctx,
            "Summarize the recommended carrier market for this E&S submission, "
            "noting any missing information for the top match.",
            facts,
            tier="standard",
        )

    async def package(self, ctx: Ctx, *args: object) -> OutputPackage:
        data, decision, draft = args[0], args[1], args[2]
        assert isinstance(data, ExtractedModel)
        assert isinstance(decision, Decision)
        assert isinstance(draft, Draft)

        ds = decision.details.get("diligent_search", {})
        payload = MarketMatchingPayload(
            submission_id=data.submission_id,
            matches=[CarrierMatchOut(**m) for m in decision.details.get("matches", [])],
            excluded=[ExcludedCarrierOut(**e) for e in decision.details.get("excluded", [])],
            diligent_search=DiligentSearchOut(**ds) if ds else DiligentSearchOut(
                required=True, on_file=0, compliant=False, note="not evaluated"
            ),
        )
        missing_info = [
            m["carrier_name"] + ": " + "; ".join(m["missing"])
            for m in decision.details.get("matches", [])
            if m["missing"]
        ]

        return OutputPackage(
            submission_id=data.submission_id,
            decision=decision,
            draft=draft,
            flags=[f["note"] for f in [ds] if f and not f.get("compliant", True)],
            missing_info=missing_info,
            citations=draft.citations,
            payload=payload.model_dump(),
        )

    async def run(self, ctx: Ctx, inp: WorkflowInput) -> OutputPackage:
        """Convenience orchestration of the full 6-step pipeline (steps 8+ —
        queue/human-action/audit — are the caller's job, per WORKFLOW_TEMPLATE.md)."""
        raw = await self.ingest(ctx, inp)
        data = await self.extract(ctx, raw)
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)
