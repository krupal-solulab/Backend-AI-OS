"""Implements ``WorkflowPipeline[OutputPackage]`` for E&S Renewal
Remarketing.

An ORCHESTRATION workflow, not a new capability (per the PRD's own §8):
RR-01/RR-02 are fresh native detection (nothing existed to port — see
``remarket_engine.py``'s docstring), RR-03/RR-07 read Market Matching's own
Carrier Appetite Profile data indirectly via the account's own renewal
context, RR-05's actual Market Matching re-invocation is a separate,
broker-approval-gated router action (not automatic here — see
``router.py``'s docstring for why), and RR-06 is a small native 2-offer
comparability check.

Two input shapes (detected from the scenario data itself, not the
filename): a trigger-decision pass (exposure/loss/incumbent-status fields
present) or a post-remarket comparison pass (``alternative_quote_received``
present — Scenario 05's shape).
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
from verticals.es.workflows.renewal_remarketing.remarket_engine import (
    ComparisonResult,
    ExposureChangeResult,
    IncumbentStatusResult,
    LossHistoryChangeResult,
    RemarketingHistoryResult,
    TriggerDecision,
    check_incumbent_status,
    compare_renewal_options,
    decide_trigger,
    detect_exposure_change,
    detect_loss_history_change,
    parse_remarketing_history,
)
from verticals.es.workflows.renewal_remarketing.scenario_loader import load_scenario
from verticals.es.workflows.renewal_remarketing.schema import (
    ComparisonOptionOut,
    ComparisonOutputOut,
    ExposureChangeOut,
    IncumbentStatusOut,
    LossHistoryChangeOut,
    ReasoningCitationOut,
    RemarketDecisionPayload,
    RemarketExecutionOut,
    TriggerDecisionOut,
    TriggerReasoningOut,
)

WORKFLOW_NAME = "renewal_remarketing"
DEFAULT_WORKFLOW_N = 16  # Workflow_16 in TEST_DATA_ROOT — see DATA_AND_FIXTURES.md

# FR-19: renewal reviews are generated on-demand (via /run) rather than by a
# real scheduled/recurring job. No workflow anywhere in this codebase has
# actual scheduler/cron infrastructure yet — every other PRD's "scheduled
# process" mention resolves to on-demand or read-time recompute in the real
# implementation (see Quote Comparison's/Binder Issuance's read-time
# recompute pattern) — so this is deliberate v1 scope, consistent with that
# project-wide precedent, not an oversight specific to this workflow.


class RenewalRemarketingPipeline:
    """One renewal-review pass. See module docstring."""

    def __init__(self, llm: LLMService, *, workflow_n: int = DEFAULT_WORKFLOW_N) -> None:
        self._llm = llm
        self._workflow_n = workflow_n
        self._context: dict[str, Any] = {}
        self._is_comparison_stage: bool = False
        self._bind_id: str | None = None
        self._named_insured: str | None = None
        self._incumbent_carrier_id: str | None = None
        self._incumbent_carrier_name: str = ""
        self._exposure: ExposureChangeResult | None = None
        self._loss: LossHistoryChangeResult | None = None
        self._incumbent: IncumbentStatusResult | None = None
        self._history: RemarketingHistoryResult | None = None
        self._trigger: TriggerDecision | None = None
        self._comparison: ComparisonResult | None = None

    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle:
        scenario_ref = inp.source_ref or inp.submission_id
        if not scenario_ref:
            raise ValueError("WorkflowInput needs source_ref or submission_id (a scenario ref)")
        context = load_scenario(self._workflow_n, scenario_ref)
        self._context = context
        self._bind_id = context.get("bind_id")
        self._named_insured = context.get("named_insured")
        self._incumbent_carrier_id = context.get("incumbent_carrier_id")
        self._incumbent_carrier_name = context.get("incumbent_carrier_name", "")
        self._is_comparison_stage = "alternative_quote_received" in context
        return RawBundle(submission_id=self._bind_id)

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        """No document extraction here — every scenario is already a
        structured JSON snapshot (unlike every prior E&S workflow, this one
        introduces no new extraction target at all)."""
        fields = [
            ExtractedValue(name="named_insured", value=self._named_insured),
            ExtractedValue(name="incumbent_carrier_name", value=self._incumbent_carrier_name),
        ]
        return ExtractedModel(submission_id=raw.submission_id, fields=fields)

    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]:
        """RR-01..RR-08 are compound orchestration/decision logic, native in
        remarket_engine.py (Option-A) — nothing fits the generic 6-check
        rules engine."""
        return []

    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision:
        context = self._context
        if self._is_comparison_stage:
            self._comparison = compare_renewal_options(
                context.get("incumbent_renewal_offer", {}), context["alternative_quote_received"]
            )
            outcome = (
                DecisionOutcome.PROCEED if self._comparison.directly_comparable
                else DecisionOutcome.REQUEST_INFO
            )
            rationale = (
                "Directly comparable" if self._comparison.directly_comparable
                else f"Material differences: {', '.join(self._comparison.material_differences)}"
            )
            return Decision(outcome=outcome, rationale=rationale)

        self._exposure = detect_exposure_change(context.get("exposure_change", {}))
        self._loss = detect_loss_history_change(context.get("expiring_term_loss_activity", ""))
        self._incumbent = check_incumbent_status(context.get("incumbent_renewal_offer", {}))
        self._history = parse_remarketing_history(context.get("remarketing_history"))
        self._trigger = decide_trigger(self._exposure, self._loss, self._incumbent, self._history)

        outcome = (
            DecisionOutcome.REQUEST_INFO if self._trigger.level != "NO_REMARKET"
            else DecisionOutcome.PROCEED
        )
        return Decision(outcome=outcome, rationale=self._trigger.reasoning)

    async def draft(self, ctx: Ctx, decision: Decision) -> Draft:
        facts = [
            ExtractedValue(name="named_insured", value=self._named_insured),
            ExtractedValue(name="incumbent_carrier_name", value=self._incumbent_carrier_name),
        ]
        if self._is_comparison_stage and self._comparison:
            facts.append(ExtractedValue(
                name="comparison_material_differences", value=self._comparison.material_differences
            ))
            if self._comparison.alternative_is_exception_based:
                facts.append(ExtractedValue(
                    name="exception_detail", value=self._comparison.exception_detail
                ))
            prompt = (
                "Summarize this renewal-alternative comparison for a wholesale broker. Never "
                "recommend the lower premium option by default if material terms differ — "
                "present the trade-off explicitly, and flag if an alternative required a "
                "manual underwriting exception."
            )
        else:
            assert self._trigger is not None
            facts.append(ExtractedValue(name="trigger_level", value=self._trigger.level))
            facts.append(ExtractedValue(name="trigger_reasoning", value=self._trigger.reasoning))
            prompt = (
                "Summarize this renewal remarket recommendation for a wholesale broker. State "
                "the trigger level plainly and ground the reasoning in the facts provided — "
                "never invent a figure or signal not listed."
            )
        return await self._llm.draft(ctx, prompt, facts, tier="standard")

    async def package(self, ctx: Ctx, *args: object) -> OutputPackage:
        data, decision, draft = args[0], args[1], args[2]
        assert isinstance(data, ExtractedModel)
        assert isinstance(decision, Decision)
        assert isinstance(draft, Draft)

        remarket_execution = RemarketExecutionOut()
        trigger_out = TriggerDecisionOut(
            level="NO_REMARKET", reasoning=TriggerReasoningOut(summary=decision.rationale or "")
        )

        if self._is_comparison_stage and self._comparison:
            incumbent = self._context.get("incumbent_renewal_offer", {})
            alternative = self._context["alternative_quote_received"]
            remarket_execution = RemarketExecutionOut(
                initiated=True,
                comparison_output=ComparisonOutputOut(
                    directly_comparable=self._comparison.directly_comparable,
                    material_differences=self._comparison.material_differences,
                    incumbent=ComparisonOptionOut(
                        carrier_name=self._incumbent_carrier_name,
                        premium=incumbent.get("premium"), limits=incumbent.get("limits"),
                        deductible=incumbent.get("deductible"),
                    ),
                    alternative=ComparisonOptionOut(
                        carrier_name=alternative.get("carrier_name", ""),
                        premium=alternative.get("premium"), limits=alternative.get("limits"),
                        deductible=alternative.get("deductible"),
                        is_exception_based=self._comparison.alternative_is_exception_based,
                        exception_detail=self._comparison.exception_detail,
                    ),
                ),
            )
            payload = RemarketDecisionPayload(
                renewal_review_id=str(uuid4()), bind_id=self._bind_id,
                named_insured=self._named_insured,
                incumbent_carrier_id=self._incumbent_carrier_id,
                incumbent_carrier_name=self._incumbent_carrier_name,
                is_comparison_stage=True,
                trigger_decision=trigger_out, remarket_execution=remarket_execution,
            )
            flags = (
                [] if self._comparison.directly_comparable
                else ["comparison_not_directly_comparable"]
            )
            if self._comparison.alternative_is_exception_based:
                flags.append("alternative_is_exception_based")
        else:
            assert (
                self._exposure and self._loss and self._incumbent
                and self._history and self._trigger
            )
            fields_changed = ["revenue"] if abs(self._exposure.pct_change) > 0.01 else []
            payload = RemarketDecisionPayload(
                renewal_review_id=str(uuid4()), bind_id=self._bind_id,
                named_insured=self._named_insured,
                incumbent_carrier_id=self._incumbent_carrier_id,
                incumbent_carrier_name=self._incumbent_carrier_name,
                is_comparison_stage=False,
                exposure_change=ExposureChangeOut(
                    fields_changed=fields_changed, material=self._exposure.material,
                    already_endorsed=self._exposure.already_endorsed,
                    pct_change=self._exposure.pct_change, note=self._exposure.note,
                ),
                loss_history_change=LossHistoryChangeOut(
                    new_claims_count=self._loss.new_claims_count,
                    favorable_resolutions_count=self._loss.favorable_resolutions_count,
                    trend=self._loss.trend,
                ),
                incumbent_status=IncumbentStatusOut(
                    renewal_terms_received=self._incumbent.renewal_terms_received,
                    days_before_expiration_at_receipt=(
                        self._incumbent.days_before_expiration_at_receipt
                    ),
                    non_response_flag=self._incumbent.non_response_flag,
                ),
                remarketing_history_detail=self._history.detail,
                trigger_decision=TriggerDecisionOut(
                    level=self._trigger.level,
                    reasoning=TriggerReasoningOut(
                        summary=self._trigger.reasoning,
                        citations=[
                            ReasoningCitationOut(claim=claim, source=source)
                            for claim, source in self._trigger.citations
                        ],
                    ),
                ),
                remarket_execution=remarket_execution,
            )
            flags = [self._trigger.level.lower()] if self._trigger.level != "NO_REMARKET" else []

        return OutputPackage(
            submission_id=self._bind_id,
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
