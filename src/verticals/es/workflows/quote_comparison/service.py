"""Implements ``WorkflowPipeline[OutputPackage]`` for E&S Quote Comparison &
Recommendation.

Unlike Package Assembly/Agent Communication, this workflow DOES ingest raw
documents (carrier response emails) and DOES run real extraction — just a
native one (``quote_parser.py``), not the shared ``ExtractionService`` (see
that module's docstring for why). ``decide()`` runs the QC-01/03/06/07
comparison/recommendation engine (``comparison_engine.py``); ``package()``
builds this workflow's output shape.

One instance per (scenario/submission) run — per-run state (parsed
responses, the reference "as of" date, the recommendation result) is stashed
on ``self``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.common.dtos import (
    Citation,
    Ctx,
    Decision,
    Draft,
    ExtractedModel,
    ExtractedValue,
    OutputPackage,
    RawBundle,
    RawDocument,
    RuleResult,
    WorkflowInput,
)
from core.common.enums import DecisionOutcome, DocumentKind
from core.config import get_settings
from core.llm.service import LLMService
from verticals.es.decision_core.carrier_profiles import CarrierProfile, load_carrier_panel
from verticals.es.workflows.quote_comparison.comparison_engine import (
    VALIDITY_URGENCY_THRESHOLD_DAYS,
    Quote,
    RecommendationResult,
    classify_declination_consistency,
    make_quotes,
    recommend,
)
from verticals.es.workflows.quote_comparison.live_ingestion import load_live_response_bundle
from verticals.es.workflows.quote_comparison.quote_parser import ParsedResponse, parse_response
from verticals.es.workflows.quote_comparison.scenario_loader import load_scenario
from verticals.es.workflows.quote_comparison.schema import (
    ComparabilityAssessmentOut,
    ComparisonPayload,
    DeductiblesOut,
    EndorsementOut,
    ExtractedQuoteOut,
    RecommendationOut,
    RecommendationReasoningOut,
    SubjectivityOut,
    UrgencyFlagOut,
)

WORKFLOW_NAME = "quote_comparison"
DEFAULT_WORKFLOW_N = 13  # Workflow_13 in TEST_DATA_ROOT — see DATA_AND_FIXTURES.md
MARKET_MATCHING_WORKFLOW_N = 10  # for QC-03's carrier-appetite-profile lookup


def _to_deductibles_out(ded: Any) -> DeductiblesOut | None:
    if ded is None:
        return None
    return DeductiblesOut(all_perils=ded.all_perils, wind_hail=ded.wind_hail)


class QuoteComparisonPipeline:
    """One (scenario/submission) comparison pass. See module docstring."""

    def __init__(
        self,
        llm: LLMService,
        *,
        workflow_n: int = DEFAULT_WORKFLOW_N,
        market_matching_workflow_n: int = MARKET_MATCHING_WORKFLOW_N,
    ) -> None:
        self._llm = llm
        self._workflow_n = workflow_n
        self._mm_workflow_n = market_matching_workflow_n
        self._submission_id: str | None = None
        self._as_of: date | None = None
        self._parsed: list[ParsedResponse] | None = None
        self._quotes: list[Quote] | None = None
        self._carrier_panel: list[CarrierProfile] | None = None
        self._result: RecommendationResult | None = None

    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle:
        scenario_ref = inp.source_ref or inp.submission_id
        if not scenario_ref:
            raise ValueError("WorkflowInput needs source_ref or submission_id (a scenario ref)")
        self._submission_id = inp.submission_id or scenario_ref

        as_of_param = (inp.params or {}).get("as_of")
        self._as_of = date.fromisoformat(as_of_param) if as_of_param else datetime.now(UTC).date()

        bundle = load_scenario(self._workflow_n, scenario_ref)
        documents = [
            RawDocument(kind=DocumentKind.EMAIL, filename=r.filename, content=r.content)
            for r in bundle.responses
        ]
        return RawBundle(submission_id=self._submission_id, documents=documents)

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        """Native parsing (quote_parser.py) — not the shared ExtractionService
        (see that module's docstring). The full structured result is stashed
        on ``self`` for decide()/package(); this returns a lighter,
        grounding-facts-only ExtractedModel for pipeline-shape compliance."""
        self._parsed = [parse_response(d.filename, d.content) for d in raw.documents]
        self._carrier_panel = load_carrier_panel(self._mm_workflow_n)

        fields: list[ExtractedValue] = []
        for parsed in self._parsed:
            cite = Citation(document_kind=DocumentKind.EMAIL, filename=parsed.filename)
            fields.append(ExtractedValue(
                name=f"quote.{parsed.carrier_name}.response_type",
                value=parsed.response_type, citation=cite,
            ))
            if parsed.response_type == "QUOTE":
                fields.append(ExtractedValue(
                    name=f"quote.{parsed.carrier_name}.premium",
                    value=parsed.premium, citation=cite,
                ))
            else:
                fields.append(ExtractedValue(
                    name=f"quote.{parsed.carrier_name}.declination_reason",
                    value=parsed.declination_reason, citation=cite,
                ))
        return ExtractedModel(submission_id=raw.submission_id, fields=fields)

    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]:
        """QC-01..QC-07 are compound comparison logic, native in
        comparison_engine.py (Option-A) — nothing here fits the generic
        6-check rules engine."""
        return []

    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision:
        assert self._parsed is not None and self._as_of is not None, "ingest()/extract() first"
        self._quotes = make_quotes(self._parsed)
        settings = get_settings()
        self._result = recommend(
            self._quotes,
            self._as_of,
            VALIDITY_URGENCY_THRESHOLD_DAYS,
            price_weight=settings.quote_rank_price_weight,
            subjectivity_penalty=settings.quote_rank_subjectivity_penalty,
        )

        viable = [q for q in self._quotes if q.parsed.response_type == "QUOTE"]
        outcome = DecisionOutcome.PROCEED if viable else DecisionOutcome.DECLINE
        return Decision(
            outcome=outcome,
            rationale=self._result.reasoning_summary,
            details={
                "output_mode": self._result.output_mode,
                "quotes_considered": [q.quote_id for q in self._quotes],
            },
        )

    async def draft(self, ctx: Ctx, decision: Decision) -> Draft:
        assert self._result is not None and self._quotes is not None
        facts = [
            ExtractedValue(name="output_mode", value=self._result.output_mode),
            ExtractedValue(name="recommendation_reasoning", value=self._result.reasoning_summary),
        ]
        for q in self._quotes:
            if q.parsed.response_type == "QUOTE":
                facts.append(ExtractedValue(
                    name=f"{q.parsed.carrier_name}.premium", value=q.parsed.premium
                ))
        for flag in self._result.urgency_flags:
            facts.append(ExtractedValue(name=f"urgency.{flag.flag_type}", value=flag.detail))

        prompt = (
            "Summarize this quote comparison/recommendation for a wholesale broker. "
            "State the recommended mode (single recommendation, multi-option trade-off, "
            "or single-quote urgency) and ground every claim in the facts provided — "
            "never invent a premium, term, or deadline not listed."
        )
        return await self._llm.draft(ctx, prompt, facts, tier="standard")

    async def package(self, ctx: Ctx, *args: object) -> OutputPackage:
        data, decision, draft = args[0], args[1], args[2]
        assert isinstance(data, ExtractedModel)
        assert isinstance(decision, Decision)
        assert isinstance(draft, Draft)
        assert self._quotes is not None and self._result is not None
        assert self._carrier_panel is not None

        quotes_out: list[ExtractedQuoteOut] = []
        for q in self._quotes:
            p = q.parsed
            consistency = (
                classify_declination_consistency(q, self._carrier_panel)
                if p.response_type == "DECLINATION"
                else None
            )
            quotes_out.append(ExtractedQuoteOut(
                quote_id=q.quote_id,
                submission_id=self._submission_id,
                carrier_name=p.carrier_name,
                response_type=p.response_type,
                premium=p.premium,
                limits=p.limits,
                deductibles=_to_deductibles_out(p.deductibles),
                key_endorsements=[
                    EndorsementOut(type=e.type, basis=e.basis) for e in p.key_endorsements
                ],
                subjectivities=[
                    SubjectivityOut(
                        description=s.description, materiality=s.materiality,
                        deadline_or_dependency=s.deadline_or_dependency,
                        is_dependency=s.is_dependency,
                    )
                    for s in p.subjectivities
                ],
                effective_date=p.effective_date,
                quote_valid_through=p.quote_valid_through,
                declination_reason=p.declination_reason,
                declination_appetite_consistency=consistency,
                source_email_reference=p.filename,
            ))

        named_insured = max(
            (p.named_insured for p in (q.parsed for q in self._quotes) if p.named_insured),
            key=len, default=None,
        )

        result = self._result
        payload = ComparisonPayload(
            submission_id=self._submission_id,
            named_insured=named_insured,
            quotes_considered=[q.quote_id for q in self._quotes],
            quotes=quotes_out,
            comparability_assessment=ComparabilityAssessmentOut(
                directly_comparable=result.comparability.directly_comparable,
                material_differences=result.comparability.material_differences,
            ),
            output_mode=result.output_mode,
            recommendation=RecommendationOut(
                primary_quote_id=result.primary_quote_id,
                reasoning=RecommendationReasoningOut(
                    summary=result.reasoning_summary,
                    # No per-claim citation list in v1 — every quote's own
                    # source_email_reference already grounds it; see FE contract note.
                    citations=[],
                ),
            ),
            urgency_flags=[
                UrgencyFlagOut(quote_id=f.quote_id, flag_type=f.flag_type, detail=f.detail)
                for f in result.urgency_flags
            ],
            processing_metadata={"last_updated_timestamp": datetime.now(UTC).isoformat()},
        )

        missing_info = [
            f"{q.parsed.carrier_name}: declined — "
            f"{q.parsed.declination_reason or 'no reason given'}"
            for q in self._quotes
            if q.parsed.response_type == "DECLINATION"
        ]

        return OutputPackage(
            submission_id=self._submission_id,
            decision=decision,
            draft=draft,
            flags=[f.flag_type for f in result.urgency_flags],
            missing_info=missing_info,
            citations=draft.citations,
            payload=payload.model_dump(),
        )

    async def run(self, ctx: Ctx, inp: WorkflowInput) -> OutputPackage:
        raw = await self.ingest(ctx, inp)
        data = await self.extract(ctx, raw)
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)

    async def ingest_live(
        self, ctx: Ctx, session: AsyncSession, submission_id: str
    ) -> RawBundle:
        """Live counterpart to ``ingest()``: raw documents are every real
        carrier-response email persisted so far for this submission (via
        ``live_ingestion.save_live_response``), not the static Workflow_13
        scenario. ``extract()`` is untouched — it already just parses
        whatever raw documents it's handed, exactly like the fixture path."""
        self._submission_id = submission_id
        self._as_of = datetime.now(UTC).date()
        return await load_live_response_bundle(session, ctx, submission_id)

    async def run_live(
        self, ctx: Ctx, session: AsyncSession, submission_id: str
    ) -> OutputPackage:
        """Live counterpart to ``run()``. extract()/decide()/draft()/package()
        are the exact same generic pipeline stages the fixture path uses — a
        submission with 0 or 1 accumulated responses so far is already handled
        safely by ``comparison_engine.recommend()``'s ``len(viable) < 2``
        branches, so no special-casing is needed here."""
        raw = await self.ingest_live(ctx, session, submission_id)
        data = await self.extract(ctx, raw)
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)
