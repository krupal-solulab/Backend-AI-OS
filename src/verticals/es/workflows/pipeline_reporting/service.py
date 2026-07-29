"""Implements ``WorkflowPipeline[OutputPackage]`` for E&S Pipeline &
Carrier Performance Reporting.

The 10th and LAST workflow on the original E&S roadmap. A pure
aggregation/reporting layer — the report "kind" (funnel, carrier
hit-rate, or remarketing value) is detected from the scenario's own JSON
shape, same discipline as Renewal Remarketing's two-shape detection.

Per the approved plan: no live cross-workflow DB aggregation is
attempted — every scenario's ``underlying_data.json`` is itself a
pre-aggregated period snapshot, and nothing in this fixture-driven
codebase has produced real "Q3 2027" activity to query. A live
`ReviewItem`/`AuditEntry` aggregator across all six prior workflows is
real, valuable future scope, not something this pass builds or can
validate against these scenarios.
"""

from __future__ import annotations

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
from core.llm.service import LLMService
from verticals.es.workflows.pipeline_reporting.live_aggregator import build_live_underlying_data
from verticals.es.workflows.pipeline_reporting.reporting_engine import (
    CarrierPerformance,
    FunnelResult,
    RemarketOutcome,
    build_carrier_performance,
    build_funnel,
    categorize_remarket_outcome,
)
from verticals.es.workflows.pipeline_reporting.scenario_loader import load_scenario
from verticals.es.workflows.pipeline_reporting.schema import (
    CarrierPerformanceOut,
    DataCompletenessOut,
    DataGapOut,
    FunnelStageOut,
    PipelineReportPayload,
    RemarketOutcomeOut,
)

WORKFLOW_NAME = "pipeline_reporting"
DEFAULT_WORKFLOW_N = 19  # Workflow_19 in TEST_DATA_ROOT — see DATA_AND_FIXTURES.md


class PipelineReportingPipeline:
    """One report-generation pass. See module docstring."""

    def __init__(self, llm: LLMService, *, workflow_n: int = DEFAULT_WORKFLOW_N) -> None:
        self._llm = llm
        self._workflow_n = workflow_n
        self._period: str = ""
        self._kind: str = "funnel"
        self._funnel: FunnelResult | None = None
        self._carriers: list[CarrierPerformance] = []
        self._remarket: list[RemarketOutcome] = []
        self._remarket_trigger_summary: dict[str, int] = {}

    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle:
        scenario_ref = inp.source_ref or inp.submission_id
        if not scenario_ref:
            raise ValueError("WorkflowInput needs source_ref or submission_id (a scenario ref)")
        context = load_scenario(self._workflow_n, scenario_ref)
        self._period = context.get("period", "")

        if "submissions_received" in context:
            self._kind = "funnel"
            self._funnel = build_funnel(context)
        elif "carrier_activity" in context:
            self._kind = "carrier"
            self._carriers = build_carrier_performance(context["carrier_activity"])
        else:
            self._kind = "remarketing"
            self._remarket = [
                categorize_remarket_outcome(o) for o in context.get("remarket_outcomes", [])
            ]
            triggers = context.get("remarket_triggers", {})
            self._remarket_trigger_summary = {
                "renewals_reviewed": context.get("renewals_reviewed", 0),
                "no_remarket": triggers.get("no_remarket", 0),
                "light_check": triggers.get("light_check", 0),
                "full_remarket": triggers.get("full_remarket", 0),
                "urgent_remarket": triggers.get("urgent_remarket", 0),
            }

        return RawBundle(submission_id=self._period)

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        """No document extraction here — pure aggregation of already-
        logged, already-structured period data (PR-01), same precedent as
        every prior already-JSON E&S workflow this session."""
        fields = [
            ExtractedValue(name="period", value=self._period),
            ExtractedValue(name="kind", value=self._kind),
        ]
        return ExtractedModel(submission_id=raw.submission_id, fields=fields)

    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]:
        """PR-01..PR-06 are compound aggregation/annotation logic, native
        in reporting_engine.py (Option-A) — nothing fits the generic
        6-check rules engine."""
        return []

    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision:
        if self._kind == "funnel" and self._funnel is not None and self._funnel.gaps:
            gap_stages = ", ".join(g.stage for g in self._funnel.gaps)
            return Decision(
                outcome=DecisionOutcome.REQUEST_INFO,
                rationale=(
                    f"Data completeness PARTIAL for {self._period} — logging gap in: "
                    f"{gap_stages}. Affected figures explicitly withheld, never estimated."
                ),
            )
        return Decision(
            outcome=DecisionOutcome.PROCEED,
            rationale=f"Report for {self._period} generated with complete underlying data.",
        )

    async def draft(self, ctx: Ctx, decision: Decision) -> Draft:
        facts = [
            ExtractedValue(name="period", value=self._period),
            ExtractedValue(name="kind", value=self._kind),
        ]
        if self._kind == "funnel" and self._funnel is not None:
            facts.append(ExtractedValue(
                name="funnel_stages",
                value=[
                    {"stage": s.stage, "count": s.count, "pct_of_prior_stage": s.pct_of_prior_stage}
                    for s in self._funnel.stages
                ],
            ))
            facts.append(ExtractedValue(
                name="gaps",
                value=[{"stage": g.stage, "reason": g.reason} for g in self._funnel.gaps],
            ))
            prompt = (
                "Summarize this pipeline funnel report for a brokerage principal. If any stage "
                "has a data gap, state plainly which stage and why, and never present or imply a "
                "figure for it — ground every number in the facts given."
            )
        elif self._kind == "carrier":
            facts.append(ExtractedValue(
                name="carriers",
                value=[
                    {
                        "carrier_name": c.carrier_name,
                        "submissions_approached": c.submissions_approached,
                        "overall_hit_rate": c.overall_hit_rate,
                        "low_volume_flag": c.low_volume_flag,
                    }
                    for c in self._carriers
                ],
            ))
            prompt = (
                "Summarize this carrier hit-rate comparison for a brokerage principal. Any "
                "low-volume carrier's figure must carry an explicit caution annotation and must "
                "never be presented as equally reliable to a higher-volume carrier's figure, even "
                "if its raw percentage is higher."
            )
        else:
            facts.append(ExtractedValue(
                name="remarket_outcomes",
                value=[
                    {
                        "account": r.account, "outcome_type": r.outcome_type,
                        "savings_amount": r.savings_amount, "note": r.note,
                    }
                    for r in self._remarket
                ],
            ))
            prompt = (
                "Summarize this remarketing value report for a brokerage principal. A $0-savings "
                "outcome that confirmed the incumbent was the best option is a legitimate, "
                "valuable result — never present it as a failure or null outcome alongside "
                "genuine savings figures."
            )
        return await self._llm.draft(ctx, prompt, facts, tier="standard")

    async def package(self, ctx: Ctx, *args: object) -> OutputPackage:
        data, decision, draft = args[0], args[1], args[2]
        assert isinstance(data, ExtractedModel)
        assert isinstance(decision, Decision)
        assert isinstance(draft, Draft)

        data_completeness = DataCompletenessOut(status="COMPLETE", gaps=[])
        funnel_out: list[FunnelStageOut] = []
        overall_conversion_pct: float | None = None
        if self._kind == "funnel" and self._funnel is not None:
            funnel_out = [
                FunnelStageOut(
                    stage=s.stage, count=s.count, pct_of_prior_stage=s.pct_of_prior_stage
                )
                for s in self._funnel.stages
            ]
            overall_conversion_pct = self._funnel.overall_conversion_pct
            if self._funnel.gaps:
                data_completeness = DataCompletenessOut(
                    status="PARTIAL",
                    gaps=[DataGapOut(stage=g.stage, reason=g.reason) for g in self._funnel.gaps],
                )

        payload = PipelineReportPayload(
            report_id=str(uuid4()),
            period=self._period,
            data_completeness=data_completeness,
            funnel=funnel_out,
            overall_conversion_pct=overall_conversion_pct,
            carrier_performance=[
                CarrierPerformanceOut(
                    carrier_name=c.carrier_name, submissions_approached=c.submissions_approached,
                    quote_rate=c.quote_rate, bind_rate=c.bind_rate,
                    overall_hit_rate=c.overall_hit_rate, low_volume_flag=c.low_volume_flag,
                )
                for c in self._carriers
            ],
            remarketing_value=[
                RemarketOutcomeOut(
                    account=r.account, trigger_level=r.trigger_level, outcome_type=r.outcome_type,
                    savings_amount=r.savings_amount, note=r.note,
                )
                for r in self._remarket
            ],
        )
        payload_dict = payload.model_dump()
        if self._remarket_trigger_summary:
            payload_dict["remarket_trigger_summary"] = self._remarket_trigger_summary

        flags = ["data_gap"] if data_completeness.status == "PARTIAL" else []
        flags += [f"low_volume:{c.carrier_name}" for c in self._carriers if c.low_volume_flag]

        return OutputPackage(
            submission_id=self._period,
            decision=decision,
            draft=draft,
            flags=flags,
            missing_info=[g.stage for g in self._funnel.gaps] if self._funnel else [],
            citations=draft.citations,
            payload=payload_dict,
        )

    async def run(self, ctx: Ctx, inp: WorkflowInput) -> OutputPackage:
        raw = await self.ingest(ctx, inp)
        data = await self.extract(ctx, raw)
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)

    async def run_live(self, ctx: Ctx, session: AsyncSession) -> OutputPackage:
        """Additive live-aggregation entry point, alongside ``run()``'s
        fixture-scenario path above. Unlike a single scenario (which always
        exercises exactly one report "kind"), this computes all three
        sections — funnel, carrier performance, remarketing value — at once
        from real cross-workflow data. See ``live_aggregator.py`` for the
        exact mapping and its honest limitations (no revenue attribution,
        savings only computed for a real recorded carrier switch)."""
        underlying = await build_live_underlying_data(session, ctx)
        self._period = "Live (current data)"
        self._kind = "funnel"  # so decide()/package()'s gap-handling logic below applies
        self._funnel = build_funnel(underlying)
        self._carriers = build_carrier_performance(underlying["carrier_activity"])
        self._remarket = [
            categorize_remarket_outcome(o) for o in underlying["remarket_outcomes"]
        ]

        raw = RawBundle(submission_id=self._period)
        data = await self.extract(ctx, raw)
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)
