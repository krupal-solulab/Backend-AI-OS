"""Implements ``WorkflowPipeline[OutputPackage]`` for E&S Endorsement /
Mid-Term Change Processing.

Like Binder & Policy Issuance, this workflow's input shape varies by stage
(see ``scenario_loader.py``): a pre-issuance pass (``bound_policy_context.json``
+ ``endorsement_request_email.txt``) or a post-issuance reconciliation pass
(``endorsement_request_sent.json`` + ``carrier_issued_endorsement.txt``).
``decide()`` branches on which is present.

Reuses Market Matching's ``CarrierProfile``/``load_carrier_panel`` data
layer directly for EP-02's appetite recheck (see
``classification_engine.py``'s docstring for why that's a different
boundary than cross-workflow-folder reuse) — the classification/
reconciliation LOGIC itself is entirely native (``classification_engine.py``).
"""

from __future__ import annotations

from datetime import date
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
from verticals.es.decision_core.carrier_profiles import CarrierProfile, load_carrier_panel
from verticals.es.workflows.endorsement.classification_engine import (
    PREMIUM_BEARING_TYPES,
    AppetiteRecheckResult,
    ClassificationResult,
    Discrepancy,
    ProrationInputs,
    appetite_recheck,
    classify,
    proration_inputs,
    reconcile_items,
)
from verticals.es.workflows.endorsement.endorsement_parser import (
    ParsedIssuedEndorsement,
    extract_requested_effective_date,
    parse_issued_endorsement,
    split_requested_items,
)
from verticals.es.workflows.endorsement.scenario_loader import ScenarioBundle, load_scenario
from verticals.es.workflows.endorsement.schema import (
    AppetiteRecheckOut,
    CarrierResponseOut,
    DiscrepancyOut,
    DraftedRequestOut,
    EndorsementRequestPayload,
    PremiumImpactOut,
    ProrationInputsOut,
    RequestedChangeOut,
)

WORKFLOW_NAME = "endorsement"
DEFAULT_WORKFLOW_N = 15  # Workflow_15 in TEST_DATA_ROOT — see DATA_AND_FIXTURES.md
MARKET_MATCHING_WORKFLOW_N = 10  # for EP-02's carrier-appetite-profile fallback


class EndorsementPipeline:
    """One endorsement-request coordination pass. See module docstring."""

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
        self._bundle: ScenarioBundle | None = None
        self._carrier_panel: list[CarrierProfile] = []
        self._parsed_issued: ParsedIssuedEndorsement | None = None
        # decide()-computed state:
        self._bind_id: str | None = None
        self._named_insured: str | None = None
        self._carrier_id: str | None = None
        self._carrier_name: str = ""
        self._change_type: str = ""
        self._change_detail: str = ""
        self._requested_effective_date: date | None = None
        self._requested_items: list[str] = []
        self._classification: ClassificationResult | None = None
        self._appetite: AppetiteRecheckResult | None = None
        self._premium_bearing: bool | None = None
        self._proration: ProrationInputs | None = None
        self._reconciliation_status: str = "PENDING"
        self._discrepancies: list[Discrepancy] = []
        self._is_reconciliation_pass: bool = False

    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle:
        scenario_ref = inp.source_ref or inp.submission_id
        if not scenario_ref:
            raise ValueError("WorkflowInput needs source_ref or submission_id (a scenario ref)")
        bundle = load_scenario(self._workflow_n, scenario_ref)
        self._bundle = bundle
        self._carrier_panel = load_carrier_panel(self._mm_workflow_n)

        context = bundle.bound_policy_context or bundle.endorsement_request_sent or {}
        self._bind_id = context.get("bind_id")
        self._named_insured = context.get("named_insured")
        self._carrier_id = context.get("carrier_id")
        self._carrier_name = context.get("carrier_name", "")
        return RawBundle(submission_id=self._bind_id)

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        """No ACORD-style document extraction here — the structured change
        request already arrives pre-extracted in ``bound_policy_context.json``
        (this workflow's only genuine parsing challenges are the issued-
        endorsement email and multi-part item splitting — see
        ``endorsement_parser.py``)."""
        assert self._bundle is not None
        bundle = self._bundle

        if bundle.carrier_issued_endorsement_text:
            self._parsed_issued = parse_issued_endorsement(bundle.carrier_issued_endorsement_text)

        fields: list[ExtractedValue] = []
        if self._parsed_issued:
            fields.append(ExtractedValue(
                name="issued.endorsement_number", value=self._parsed_issued.endorsement_number
            ))
        return ExtractedModel(submission_id=raw.submission_id, fields=fields)

    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]:
        """EP-01..EP-06 are compound classification/reconciliation logic,
        native in classification_engine.py (Option-A) — nothing fits the
        generic 6-check rules engine."""
        return []

    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision:
        assert self._bundle is not None
        bundle = self._bundle

        if bundle.endorsement_request_sent is not None:
            self._decide_reconciliation_pass(bundle.endorsement_request_sent)
        elif bundle.bound_policy_context is not None:
            self._decide_pre_issuance_pass(bundle.bound_policy_context)
        else:
            raise ValueError(
                "scenario has neither bound_policy_context nor endorsement_request_sent"
            )

        outcome = DecisionOutcome.PROCEED
        needs_attention = (
            self._reconciliation_status == "DISCREPANCY_FLAGGED"
            or (self._appetite is not None and self._appetite.outcome == "APPETITE_UNKNOWN")
            or (
                self._classification is not None
                and self._classification.classification == "UNDERWRITING_REVIEW_REQUIRED"
            )
        )
        if needs_attention:
            outcome = DecisionOutcome.REQUEST_INFO

        rationale = (
            self._classification.reasoning if self._classification
            else f"reconciliation={self._reconciliation_status}"
        )
        return Decision(outcome=outcome, rationale=rationale)

    def _decide_pre_issuance_pass(self, context: dict[str, Any]) -> None:
        change = context.get("requested_change", {})
        self._change_type = change.get("type", "other")
        self._change_detail = change.get("detail", "")
        email = self._bundle.endorsement_request_email_text if self._bundle else None
        self._requested_effective_date = extract_requested_effective_date(
            self._change_detail, email or ""
        )
        self._requested_items = split_requested_items(self._change_detail)

        current_terms = context.get("current_terms", {})
        current_premium = current_terms.get("premium")
        self._classification = classify(self._change_type, self._change_detail, current_premium)
        self._appetite = appetite_recheck(
            self._change_type, self._change_detail, context, self._carrier_panel
        )

        self._premium_bearing = self._change_type in PREMIUM_BEARING_TYPES

        if self._premium_bearing and current_terms.get("effective_date") and current_terms.get(
            "expiration_date"
        ):
            effective = date.fromisoformat(current_terms["effective_date"])
            expiration = date.fromisoformat(current_terms["expiration_date"])
            reference = self._requested_effective_date or context.get("current_date")
            reference = date.fromisoformat(reference) if isinstance(reference, str) else reference
            if reference is not None:
                self._proration = proration_inputs(effective, expiration, reference)

    def _decide_reconciliation_pass(self, sent: dict[str, Any]) -> None:
        change = sent.get("requested_change", {})
        self._change_type = change.get("type", "other")
        self._change_detail = change.get("detail", "")
        self._requested_items = split_requested_items(self._change_detail)
        self._is_reconciliation_pass = True

        if self._parsed_issued is not None:
            discrepancies = reconcile_items(self._requested_items, self._parsed_issued.issued_items)
            self._discrepancies = discrepancies
            self._reconciliation_status = "DISCREPANCY_FLAGGED" if discrepancies else "CLEAN"

    async def draft(self, ctx: Ctx, decision: Decision) -> Draft:
        facts = [
            ExtractedValue(name="named_insured", value=self._named_insured),
            ExtractedValue(name="carrier_name", value=self._carrier_name),
            ExtractedValue(name="requested_change_type", value=self._change_type),
            ExtractedValue(name="requested_change_detail", value=self._change_detail),
        ]
        if self._appetite:
            facts.append(ExtractedValue(name="appetite_recheck", value=self._appetite.detail))
        if self._proration:
            facts.append(ExtractedValue(
                name="proration_inputs",
                value=f"{self._proration.days_elapsed} days elapsed, "
                f"{self._proration.days_remaining} days remaining of "
                f"{self._proration.term_total_days} total",
            ))
        if self._discrepancies:
            facts.append(ExtractedValue(
                name="discrepancies",
                value=[d.requested_item for d in self._discrepancies],
            ))

        if self._is_reconciliation_pass:
            prompt = (
                "Summarize this issued-endorsement reconciliation for a wholesale broker. "
                "State plainly whether every requested item was issued or whether something "
                "is missing — ground every claim in the facts, never invent an item."
            )
        elif self._classification and self._classification.classification == "ROUTINE":
            prompt = (
                "Draft a routine endorsement request to the carrier, matching the request "
                "exactly. Ground every claim in the facts provided."
            )
        else:
            prompt = (
                "Draft this as a submission for underwriting judgment, not a standard "
                "'please process this endorsement' request — include full supporting "
                "context (current terms, appetite considerations, loss history where "
                "relevant). If the appetite recheck is unknown, frame this as a direct "
                "question to the carrier's underwriting team, not an assumed-processable "
                "request."
            )
        return await self._llm.draft(ctx, prompt, facts, tier="standard")

    async def package(self, ctx: Ctx, *args: object) -> OutputPackage:
        data, decision, draft = args[0], args[1], args[2]
        assert isinstance(data, ExtractedModel)
        assert isinstance(decision, Decision)
        assert isinstance(draft, Draft)

        appetite_out = (
            AppetiteRecheckOut(
                applicable=self._appetite.applicable, outcome=self._appetite.outcome,
                detail=self._appetite.detail,
                state_licensing_clarification_needed=(
                    self._appetite.state_licensing_clarification_needed
                ),
            )
            if self._appetite else AppetiteRecheckOut(applicable=False)
        )
        premium_out = PremiumImpactOut(
            premium_bearing=self._premium_bearing,
            proration_inputs=(
                ProrationInputsOut(
                    days_elapsed=self._proration.days_elapsed,
                    days_remaining=self._proration.days_remaining,
                    term_total_days=self._proration.term_total_days,
                    unusual_timing_flag=self._proration.unusual_timing_flag,
                )
                if self._proration else None
            ),
        )
        downstream_fires = self._is_reconciliation_pass and self._reconciliation_status == "CLEAN"

        payload = EndorsementRequestPayload(
            endorsement_request_id=str(uuid4()),
            bind_id=self._bind_id,
            named_insured=self._named_insured,
            carrier_id=self._carrier_id,
            carrier_name=self._carrier_name,
            requested_change=RequestedChangeOut(
                type=self._change_type, detail=self._change_detail,
                requested_effective_date=(
                    self._requested_effective_date.isoformat()
                    if self._requested_effective_date else None
                ),
            ),
            requested_items=self._requested_items,
            classification=(
                self._classification.classification if self._classification
                else "UNDERWRITING_REVIEW_REQUIRED"
            ),
            classification_reasoning=self._classification.reasoning if self._classification else "",
            appetite_recheck=appetite_out,
            premium_impact=premium_out,
            drafted_request=DraftedRequestOut(body=draft.text, citations=[]),
            carrier_response=CarrierResponseOut(
                endorsement_number=(
                    self._parsed_issued.endorsement_number if self._parsed_issued else None
                ),
                issued_items=self._parsed_issued.issued_items if self._parsed_issued else [],
                reconciliation_status=self._reconciliation_status,
                discrepancy_detail=[
                    DiscrepancyOut(requested_item=d.requested_item, issued_item=d.issued_item)
                    for d in self._discrepancies
                ],
            ),
            downstream_trigger_fired=downstream_fires,
            status_log=[],
        )

        flags = []
        if self._appetite and self._appetite.outcome == "APPETITE_UNKNOWN":
            flags.append("appetite_unknown")
        if self._appetite and self._appetite.state_licensing_clarification_needed:
            flags.append("state_licensing_clarification_needed")
        if self._reconciliation_status == "DISCREPANCY_FLAGGED":
            flags.append("reconciliation_discrepancy")

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
