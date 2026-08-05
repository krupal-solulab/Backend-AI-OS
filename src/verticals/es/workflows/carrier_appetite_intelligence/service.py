"""Implements ``WorkflowPipeline[OutputPackage]`` for E&S Carrier Appetite
Intelligence Tracking.

Per the approved plan: this workflow resolves each carrier's REAL stated
profile from Market Matching's own Workflow_10 ``carrier_profiles/*.json``
fixtures (via ``decision_core.carrier_profiles.load_carrier_panel`` —
E&S-vertical-shared DATA-layer reuse, same precedent as Endorsement's
reuse of that module) rather than relying only on a scenario's own inline
``stated_profile`` (only Scenario 02's fixture embeds one). CI-03's
metadata refresh is COMPUTED and RECORDED in this workflow's own payload
— no mutable Carrier Appetite Profile store exists anywhere in this
codebase to write into, so nothing here ever calls back into
``decision_core`` to mutate a profile; that write path simply doesn't
exist, by design (see ``schema.py``'s module docstring).

FR-1's periodic-batch framing is handled the same way every prior
deferred-scheduled-job decision in this vertical was: no new Arq/cron
infra, a manual on-demand ``/run`` per carrier/class scenario_ref (this
is the 5th such deferral in this vertical, same reasoning each time).
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from core.config import get_settings
from core.llm.service import LLMService
from verticals.es.decision_core.carrier_profiles import load_carrier_panel
from verticals.es.workflows.carrier_appetite_intelligence.consistency_engine import (
    PatternResult,
    compute_metadata_refresh,
    score_pattern,
)
from verticals.es.workflows.carrier_appetite_intelligence.live_signal_builder import (
    build_live_signal_log,
)
from verticals.es.workflows.carrier_appetite_intelligence.scenario_loader import load_scenario
from verticals.es.workflows.carrier_appetite_intelligence.schema import (
    CarrierAppetiteEvaluationPayload,
    EvidenceItemOut,
    MetadataRefreshOut,
)

WORKFLOW_NAME = "carrier_appetite_intelligence"
DEFAULT_WORKFLOW_N = 18  # Workflow_18 in TEST_DATA_ROOT — see DATA_AND_FIXTURES.md
_CARRIER_PANEL_WORKFLOW_N = 10  # real Carrier Appetite Profiles live in Workflow_10's fixtures


class CarrierAppetiteIntelligencePipeline:
    """One carrier/class evaluation pass. See module docstring."""

    def __init__(self, llm: LLMService, *, workflow_n: int = DEFAULT_WORKFLOW_N) -> None:
        self._llm = llm
        self._workflow_n = workflow_n
        self._carrier_id: str = ""
        self._carrier_name: str = ""
        self._class_code: str = ""
        self._current_confidence: str = "medium"
        self._result: PatternResult | None = None
        self._suggested_action: str | None = None
        self._metadata_refresh: dict[str, str] | None = None

    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle:
        scenario_ref = inp.source_ref or inp.submission_id
        if not scenario_ref:
            raise ValueError("WorkflowInput needs source_ref or submission_id (a scenario ref)")
        context = load_scenario(self._workflow_n, scenario_ref)
        self._carrier_id = context["carrier_id"]
        self._carrier_name = context.get("carrier_name", "")
        self._class_code = context["class_code"]

        # Real profile first (Workflow_10's actual fixtures), falling back to
        # the scenario's own inline `stated_profile` only if the carrier isn't
        # found in the real panel.
        panel = load_carrier_panel(_CARRIER_PANEL_WORKFLOW_N)
        real_profile = next((p for p in panel if p.carrier_id == self._carrier_id), None)
        if real_profile is not None:
            self._current_confidence = real_profile.appetite_confidence
        else:
            self._current_confidence = (context.get("stated_profile") or {}).get(
                "appetite_confidence", "medium"
            )

        self._result = score_pattern(
            context["observed_outcomes"],
            min_total_outcomes=get_settings().carrier_appetite_min_total_outcomes,
        )
        return RawBundle(submission_id=f"{self._carrier_id}:{self._class_code}")

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        """No document extraction here — pure aggregation of already-
        logged structured signals (CI-01), same precedent as Renewal
        Remarketing/Diligent Search."""
        fields = [
            ExtractedValue(name="carrier_id", value=self._carrier_id),
            ExtractedValue(name="class_code", value=self._class_code),
        ]
        return ExtractedModel(submission_id=raw.submission_id, fields=fields)

    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]:
        """CI-01..CI-05 are compound signal-scoring logic, native in
        consistency_engine.py (Option-A) — nothing fits the generic
        6-check rules engine."""
        return []

    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision:
        assert self._result is not None
        result = self._result

        if result.pattern_type == "CONFIRMED_CONSISTENT":
            self._metadata_refresh = compute_metadata_refresh(
                self._current_confidence, datetime.now(UTC).date()
            )
            rationale = (
                f"{result.total_outcomes} of {result.total_outcomes} observed outcomes fully "
                "consistent with stated profile — metadata confidence/recency refreshed, no "
                "substantive profile field touched."
            )
            outcome = DecisionOutcome.PROCEED
        elif result.pattern_type == "GENUINE_INCONSISTENCY":
            rationale = (
                f"{result.class_level_inconsistent_count} of the last outcomes are inconsistent "
                "with an explicit class-level stated reason — a genuine appetite-shift pattern, "
                "not normal account-specific variance. Suggestion requires human review."
            )
            outcome = DecisionOutcome.REQUEST_INFO
        else:
            rationale = (
                f"Insufficient signal ({result.total_outcomes} outcome(s), "
                f"{result.class_level_inconsistent_count} class-level-inconsistent) — suppressed "
                "by default, per this workflow's conservative scope."
            )
            outcome = DecisionOutcome.PROCEED

        return Decision(outcome=outcome, rationale=rationale)

    async def draft(self, ctx: Ctx, decision: Decision) -> Draft:
        assert self._result is not None
        result = self._result
        facts = [
            ExtractedValue(name="carrier_name", value=self._carrier_name),
            ExtractedValue(name="class_code", value=self._class_code),
            ExtractedValue(name="pattern_type", value=result.pattern_type),
            ExtractedValue(name="current_appetite_confidence", value=self._current_confidence),
            ExtractedValue(
                name="evidence",
                value=[
                    {
                        "submission_id": e.submission_id,
                        "outcome": e.outcome,
                        "date": e.date,
                        "stated_reason": e.stated_reason,
                        "reason_scope": e.reason_scope,
                    }
                    for e in result.evidence
                ],
            ),
        ]
        if result.pattern_type == "GENUINE_INCONSISTENCY":
            prompt = (
                "A carrier's observed outcomes appear inconsistent with its stated appetite "
                "profile for this class. Draft a suggestion for human review recommending "
                "reconsideration of the accepted-class listing or confidence level — ground it "
                "ONLY in the evidence given (dates, outcomes, stated reasons), never invent a "
                "submission or reason not listed. This is a SUGGESTION only — never state that "
                "the profile has been changed."
            )
        elif result.pattern_type == "CONFIRMED_CONSISTENT":
            prompt = (
                "Summarize, for an underwriting manager, that this carrier's observed outcomes "
                "consistently confirm its stated appetite profile for this class, and that only "
                "confidence/recency metadata was refreshed — never imply any accepted/excluded "
                "class list changed."
            )
        else:
            prompt = (
                "Summarize, for an underwriting manager, why this carrier/class evaluation "
                "produced no suggestion (insufficient volume or non-class-level variance) — "
                "ground it only in the evidence given."
            )
        draft = await self._llm.draft(ctx, prompt, facts, tier="standard")
        if result.pattern_type == "GENUINE_INCONSISTENCY":
            self._suggested_action = draft.text
        return draft

    async def package(self, ctx: Ctx, *args: object) -> OutputPackage:
        data, decision, draft = args[0], args[1], args[2]
        assert isinstance(data, ExtractedModel)
        assert isinstance(decision, Decision)
        assert isinstance(draft, Draft)
        assert self._result is not None
        result = self._result

        status = {
            "CONFIRMED_CONSISTENT": "METADATA_AUTO_UPDATED",
            "GENUINE_INCONSISTENCY": "PENDING_REVIEW",
            "INSUFFICIENT_SIGNAL": "SUPPRESSED",
        }[result.pattern_type]

        payload = CarrierAppetiteEvaluationPayload(
            suggestion_id=str(uuid4()),
            carrier_id=self._carrier_id,
            carrier_name=self._carrier_name,
            class_code=self._class_code,
            evidence=[
                EvidenceItemOut(
                    submission_id=e.submission_id, outcome=e.outcome, date=e.date,
                    stated_reason=e.stated_reason, reason_scope=e.reason_scope,
                )
                for e in result.evidence
            ],
            pattern_type=result.pattern_type,
            suggested_action=self._suggested_action,
            status=status,
            metadata_refresh=(
                MetadataRefreshOut(**self._metadata_refresh) if self._metadata_refresh else None
            ),
        )
        flags = [result.pattern_type.lower()]

        return OutputPackage(
            submission_id=f"{self._carrier_id}:{self._class_code}",
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

    async def run_live(self, ctx: Ctx, session: AsyncSession, carrier_name: str) -> OutputPackage:
        """Additive live-aggregation entry point, alongside ``run()``'s
        fixture-scenario path above — evaluates one carrier's REAL
        declination-consistency signals, already logged by Quote
        Comparison, instead of a ``signal_log.json`` fixture. See
        ``live_signal_builder.py`` for the exact mapping and its honest
        limitations (no class_code, approximate date, name-keyed join)."""
        context = await build_live_signal_log(session, ctx, carrier_name)
        self._carrier_id = context["carrier_id"]
        self._carrier_name = context["carrier_name"]
        self._class_code = context["class_code"]

        panel = load_carrier_panel(_CARRIER_PANEL_WORKFLOW_N)
        real_profile = next((p for p in panel if p.carrier_id == self._carrier_id), None)
        self._current_confidence = (
            real_profile.appetite_confidence if real_profile is not None else "medium"
        )

        self._result = score_pattern(
            context["observed_outcomes"],
            min_total_outcomes=get_settings().carrier_appetite_min_total_outcomes,
        )
        raw = RawBundle(submission_id=f"{self._carrier_id}:{self._class_code}")
        data = await self.extract(ctx, raw)
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)
