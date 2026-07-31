"""Implements ``WorkflowPipeline[OutputPackage]`` for E&S Binder & Policy
Issuance Coordination.

This workflow's input shape genuinely varies by lifecycle stage (see
``scenario_loader.py``): a pre-bind pass (``broker_bind_instruction.json`` +
optional ``carrier_bind_confirmation.txt``) or a post-issuance pass
(``bind_record.json`` + optional ``issued_policy_document_extract.txt``).
``decide()`` branches on which is present rather than forcing one shape.

Two NEW extraction targets (carrier bind confirmations, issued policy
declarations pages) are parsed natively (``bind_parser.py``) — not the
shared ``ExtractionService``, not quote_comparison's internals (see that
module's docstring).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

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
from core.llm.service import LLMService
from verticals.es.workflows.binder_issuance.bind_parser import (
    ParsedBindConfirmation,
    ParsedIssuedPolicy,
    parse_bind_confirmation,
    parse_issued_policy,
)
from verticals.es.workflows.binder_issuance.coordination_engine import (
    BindTerms,
    Discrepancy,
    OngoingObligation,
    Subjectivity,
    bind_terms_from_dict,
    blocking_pre_bind_items,
    classify_subjectivities,
    is_overdue,
    issuance_expected_by,
    post_bind_ongoing_obligations,
    reconcile,
    reminder_due,
)
from verticals.es.workflows.binder_issuance.scenario_loader import ScenarioBundle, load_scenario
from verticals.es.workflows.binder_issuance.schema import (
    BindCoordinationPayload,
    BindTermsOut,
    CarrierConfirmationOut,
    DiscrepancyOut,
    DownstreamTriggersFiredOut,
    IssuedPolicyReconciliationOut,
    OngoingObligationOut,
    PolicyIssuanceOut,
    SubjectivityOut,
)

WORKFLOW_NAME = "binder_issuance"
DEFAULT_WORKFLOW_N = 14  # Workflow_14 in TEST_DATA_ROOT — see DATA_AND_FIXTURES.md


def _terms_out(terms: BindTerms | None) -> BindTermsOut:
    if terms is None:
        return BindTermsOut()
    return BindTermsOut(
        premium=terms.premium, limits=terms.limits_display,
        deductible_all_perils=terms.deductible_all_perils,
        deductible_wind_hail=terms.deductible_wind_hail,
        effective_date=terms.effective_date.isoformat() if terms.effective_date else None,
    )


def _discrepancies_out(items: list[Discrepancy]) -> list[DiscrepancyOut]:
    return [
        DiscrepancyOut(field=d.field, requested_or_bound=d.requested_or_bound,
                        confirmed_or_issued=d.confirmed_or_issued)
        for d in items
    ]


class BinderIssuancePipeline:
    """One (submission/bind) coordination pass. See module docstring."""

    def __init__(self, llm: LLMService, *, workflow_n: int = DEFAULT_WORKFLOW_N) -> None:
        self._llm = llm
        self._workflow_n = workflow_n
        self._submission_id: str | None = None
        self._as_of: date | None = None
        self._bundle: ScenarioBundle | None = None
        self._parsed_confirmation: ParsedBindConfirmation | None = None
        self._parsed_policy: ParsedIssuedPolicy | None = None
        # decide()-computed state, read by package():
        self._named_insured: str | None = None
        self._carrier_id: str | None = None
        self._carrier_name: str | None = None
        self._requested_terms: BindTerms | None = None
        self._subjectivities: list[Subjectivity] = []
        self._bind_order_status: str = "READY"
        self._reconciliation_status: str = "PENDING"
        self._confirmation_discrepancies: list[Discrepancy] = []
        self._confirmed_terms: BindTerms | None = None
        self._binder_number: str | None = None
        self._issuance_timeline_days: int | None = None
        self._issuance_is_default_assumption: bool = False
        self._issuance_expected_by: date | None = None
        self._documents_received: bool = False
        self._issued_policy_status: str = "NOT_YET_RECEIVED"
        self._issued_policy_discrepancies: list[Discrepancy] = []
        self._ongoing_obligations: list[OngoingObligation] = []
        self._bound_terms: BindTerms | None = None

    async def ingest(self, ctx: Ctx, inp: WorkflowInput) -> RawBundle:
        scenario_ref = inp.source_ref or inp.submission_id
        if not scenario_ref:
            raise ValueError("WorkflowInput needs source_ref or submission_id (a scenario ref)")
        as_of_param = (inp.params or {}).get("as_of")
        self._as_of = date.fromisoformat(as_of_param) if as_of_param else datetime.now(UTC).date()

        bundle = load_scenario(self._workflow_n, scenario_ref)
        self._bundle = bundle
        instr = bundle.broker_bind_instruction or bundle.bind_record or {}
        self._submission_id = instr.get("submission_id")

        documents = []
        if bundle.carrier_bind_confirmation_text:
            documents.append(RawDocument(
                kind=DocumentKind.EMAIL, filename="carrier_bind_confirmation.txt",
                content=bundle.carrier_bind_confirmation_text,
            ))
        if bundle.issued_policy_text:
            documents.append(RawDocument(
                kind=DocumentKind.OTHER, filename="issued_policy_document_extract.txt",
                content=bundle.issued_policy_text,
            ))
        return RawBundle(submission_id=self._submission_id, documents=documents)

    async def extract(self, ctx: Ctx, raw: RawBundle) -> ExtractedModel:
        """Native parsing (bind_parser.py) of whichever raw documents this
        scenario stage actually has — a lighter, grounding-facts-only
        ExtractedModel is returned for pipeline-shape compliance; the full
        structured result is stashed on ``self`` for decide()/package()."""
        for doc in raw.documents:
            if doc.filename == "carrier_bind_confirmation.txt":
                self._parsed_confirmation = parse_bind_confirmation(doc.content)
            elif doc.filename == "issued_policy_document_extract.txt":
                self._parsed_policy = parse_issued_policy(doc.content)

        fields: list[ExtractedValue] = []
        if self._parsed_confirmation:
            cite = Citation(
                document_kind=DocumentKind.EMAIL, filename="carrier_bind_confirmation.txt"
            )
            fields.append(ExtractedValue(
                name="confirmation.premium",
                value=self._parsed_confirmation.premium, citation=cite,
            ))
        if self._parsed_policy:
            cite = Citation(
                document_kind=DocumentKind.OTHER, filename="issued_policy_document_extract.txt"
            )
            fields.append(ExtractedValue(
                name="issued_policy.premium", value=self._parsed_policy.premium, citation=cite
            ))
        return ExtractedModel(submission_id=raw.submission_id, fields=fields)

    async def validate(self, ctx: Ctx, data: ExtractedModel) -> list[RuleResult]:
        """BI-01..BI-07 are compound lifecycle/reconciliation logic, native
        in coordination_engine.py (Option-A) — nothing fits the generic
        6-check rules engine."""
        return []

    async def decide(self, ctx: Ctx, data: ExtractedModel) -> Decision:
        assert self._bundle is not None and self._as_of is not None
        bundle = self._bundle

        if bundle.broker_bind_instruction:
            self._decide_pre_bind_stage(bundle.broker_bind_instruction)
        elif bundle.bind_record:
            self._decide_issuance_stage(bundle.bind_record)
        else:
            raise ValueError("scenario has neither broker_bind_instruction nor bind_record")

        rationale_parts = [f"bind_order_status={self._bind_order_status}"]
        if self._reconciliation_status != "PENDING":
            rationale_parts.append(f"confirmation_reconciliation={self._reconciliation_status}")
        if self._issued_policy_status != "NOT_YET_RECEIVED":
            rationale_parts.append(f"issued_policy_reconciliation={self._issued_policy_status}")

        outcome = DecisionOutcome.PROCEED
        needs_attention = (
            self._bind_order_status == "BLOCKED"
            or self._reconciliation_status == "DISCREPANCY_FLAGGED"
            or self._issued_policy_status == "POLICY_DISCREPANCY_FLAGGED"
        )
        if needs_attention:
            outcome = DecisionOutcome.REQUEST_INFO

        return Decision(outcome=outcome, rationale="; ".join(rationale_parts))

    def _decide_pre_bind_stage(self, instr: dict[str, Any]) -> None:
        self._named_insured = instr.get("named_insured")
        self._carrier_id = instr.get("carrier_id")
        self._carrier_name = instr.get("carrier_name")
        self._requested_terms = bind_terms_from_dict(instr.get("bind_terms_requested", {}))
        self._subjectivities = classify_subjectivities(instr.get("subjectivities_at_selection", []))

        blocking = blocking_pre_bind_items(self._subjectivities)
        if blocking:
            self._bind_order_status = "BLOCKED"
            return  # BI-02: never proceeds to confirmation reconciliation while blocked

        if self._parsed_confirmation is None:
            self._bind_order_status = "READY"
            return

        self._bind_order_status = "SENT"
        parsed = self._parsed_confirmation
        self._binder_number = parsed.binder_number
        self._confirmed_terms = BindTerms(
            premium=parsed.premium, limits_display=parsed.limits_display,
            deductible_all_perils=parsed.deductible_all_perils,
            deductible_wind_hail=parsed.deductible_wind_hail,
            effective_date=parsed.effective_date,
        )
        self._confirmation_discrepancies = reconcile(self._requested_terms, self._confirmed_terms)
        self._reconciliation_status = (
            "CLEAN" if not self._confirmation_discrepancies else "DISCREPANCY_FLAGGED"
        )
        self._bound_terms = self._confirmed_terms  # BI-05's baseline is the CONFIRMED terms

        self._ongoing_obligations = post_bind_ongoing_obligations(
            self._subjectivities, self._requested_terms.effective_date
        )

        # BI-05: a real issued policy attached alongside this confirmation
        # (live "attach policy" re-run, via run_live_update — never present
        # here via the fixture path, which always keeps these as two
        # separate scenario stages; see scenario_loader.py's docstring).
        # Identical to _decide_issuance_stage's own issued-policy handling
        # below, just reached from the pre-bind branch instead.
        if self._parsed_policy is not None:
            issued = BindTerms(
                premium=self._parsed_policy.premium,
                limits_display=self._parsed_policy.limits_display,
                deductible_all_perils=self._parsed_policy.deductible_all_perils,
                deductible_wind_hail=self._parsed_policy.deductible_wind_hail,
                effective_date=self._parsed_policy.effective_date,
            )
            self._issued_policy_discrepancies = reconcile(self._bound_terms, issued)
            self._issued_policy_status = (
                "CLEAN" if not self._issued_policy_discrepancies else "POLICY_DISCREPANCY_FLAGGED"
            )
            self._documents_received = True

        if parsed.confirmation_date is not None:
            self._issuance_timeline_days = parsed.stated_issuance_timeline_days
            expected_by, is_default = issuance_expected_by(
                parsed.confirmation_date, parsed.stated_issuance_timeline_days
            )
            self._issuance_expected_by = expected_by
            self._issuance_is_default_assumption = is_default

    def _decide_issuance_stage(self, record: dict[str, Any]) -> None:
        self._named_insured = record.get("named_insured")
        self._carrier_id = record.get("carrier_id")
        self._carrier_name = record.get("carrier_name")
        self._binder_number = record.get("binder_number")
        self._bind_order_status = "SENT"
        self._reconciliation_status = "CLEAN"  # a bind_record is, by definition, already confirmed

        bound = bind_terms_from_dict(record.get("bound_terms_confirmed", {}))
        self._bound_terms = bound
        self._requested_terms = bound  # no separate pre-bind request tracked at this stage

        if record.get("bind_confirmed_date"):
            confirmed_date = date.fromisoformat(record["bind_confirmed_date"])
            timeline_days = record.get("carrier_stated_issuance_timeline_days")
            self._issuance_timeline_days = timeline_days
            expected_by, is_default = issuance_expected_by(confirmed_date, timeline_days)
            self._issuance_expected_by = expected_by
            self._issuance_is_default_assumption = is_default
            self._documents_received = bool(record.get("policy_documents_received", False))

        if self._parsed_policy is not None:
            issued = BindTerms(
                premium=self._parsed_policy.premium,
                limits_display=self._parsed_policy.limits_display,
                deductible_all_perils=self._parsed_policy.deductible_all_perils,
                deductible_wind_hail=self._parsed_policy.deductible_wind_hail,
                effective_date=self._parsed_policy.effective_date,
            )
            self._issued_policy_discrepancies = reconcile(bound, issued)
            self._issued_policy_status = (
                "CLEAN" if not self._issued_policy_discrepancies else "POLICY_DISCREPANCY_FLAGGED"
            )
            self._documents_received = True

    async def draft(self, ctx: Ctx, decision: Decision) -> Draft:
        facts = [
            ExtractedValue(name="named_insured", value=self._named_insured),
            ExtractedValue(name="carrier_name", value=self._carrier_name),
            ExtractedValue(name="bind_order_status", value=self._bind_order_status),
        ]
        if self._reconciliation_status != "PENDING":
            facts.append(ExtractedValue(
                name="confirmation_reconciliation_status", value=self._reconciliation_status
            ))
        if self._confirmation_discrepancies:
            facts.append(ExtractedValue(
                name="confirmation_discrepancies",
                value=[f"{d.field}: requested {d.requested_or_bound} vs confirmed "
                       f"{d.confirmed_or_issued}" for d in self._confirmation_discrepancies],
            ))
        if self._issued_policy_discrepancies:
            facts.append(ExtractedValue(
                name="issued_policy_discrepancies",
                value=[f"{d.field}: bound {d.requested_or_bound} vs issued "
                       f"{d.confirmed_or_issued}" for d in self._issued_policy_discrepancies],
            ))

        prompt = (
            "Summarize this bind/issuance coordination status for a wholesale broker. "
            "State clearly whether the bind is blocked, clean, or has a flagged "
            "discrepancy — ground every claim in the facts provided, never invent a "
            "term or date not listed."
        )
        return await self._llm.draft(ctx, prompt, facts, tier="standard")

    async def package(self, ctx: Ctx, *args: object) -> OutputPackage:
        data, decision, draft = args[0], args[1], args[2]
        assert isinstance(data, ExtractedModel)
        assert isinstance(decision, Decision)
        assert isinstance(draft, Draft)

        as_of = self._as_of
        assert as_of is not None
        overdue = False
        if self._issuance_expected_by is not None:
            overdue = is_overdue(self._issuance_expected_by, as_of, self._documents_received)

        obligations_out = [
            OngoingObligationOut(
                description=o.description,
                due_date=o.due_date.isoformat() if o.due_date else None,
                status=o.status,
                reminder_due=reminder_due(o, as_of),
            )
            for o in self._ongoing_obligations
        ]

        placement_confirmation_fires = (
            self._bind_order_status == "SENT" and self._reconciliation_status == "CLEAN"
        )
        policy_documents_delivered_fires = self._issued_policy_status == "CLEAN"

        payload = BindCoordinationPayload(
            bind_id=str(uuid4()),
            submission_id=self._submission_id,
            named_insured=self._named_insured,
            carrier_id=self._carrier_id,
            carrier_name=self._carrier_name or "",
            requested_bind_terms=_terms_out(self._requested_terms),
            pre_bind_subjectivities=[
                SubjectivityOut(
                    description=s.description, materiality=s.materiality,
                    lifecycle_stage=s.lifecycle_stage,
                    status="cleared" if s.cleared else "open",
                )
                for s in self._subjectivities
            ],
            bind_order_status=self._bind_order_status,
            carrier_confirmation=CarrierConfirmationOut(
                binder_number=self._binder_number,
                confirmed_terms=(
                    _terms_out(self._confirmed_terms) if self._confirmed_terms else None
                ),
                reconciliation_status=self._reconciliation_status,
                discrepancy_detail=_discrepancies_out(self._confirmation_discrepancies),
            ),
            policy_issuance=PolicyIssuanceOut(
                carrier_stated_timeline_days=self._issuance_timeline_days,
                timeline_is_assumed_default=self._issuance_is_default_assumption,
                expected_by_date=(
                    self._issuance_expected_by.isoformat() if self._issuance_expected_by else None
                ),
                documents_received=self._documents_received,
                overdue_alert_fired=overdue,
            ),
            issued_policy_reconciliation=IssuedPolicyReconciliationOut(
                status=self._issued_policy_status,
                discrepancy_detail=_discrepancies_out(self._issued_policy_discrepancies),
            ),
            post_bind_ongoing_obligations=obligations_out,
            downstream_triggers_fired=DownstreamTriggersFiredOut(
                placement_confirmation=placement_confirmation_fires,
                policy_documents_delivered=policy_documents_delivered_fires,
            ),
            status_log=[],
        )

        flags = []
        if self._bind_order_status == "BLOCKED":
            flags.append("bind_blocked")
        if self._reconciliation_status == "DISCREPANCY_FLAGGED":
            flags.append("confirmation_discrepancy")
        if self._issued_policy_status == "POLICY_DISCREPANCY_FLAGGED":
            flags.append("policy_discrepancy")
        if overdue:
            flags.append("issuance_overdue")

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

    async def run_live(self, ctx: Ctx, broker_bind_instruction: dict[str, Any]) -> OutputPackage:
        """Additive entry point, alongside ``run()``'s fixture-scenario path
        above — starts a real pre-bind pass from an already-built
        ``broker_bind_instruction`` dict (see
        ``live_ingestion.build_broker_bind_instruction_from_quote``) instead
        of loading a Workflow_14 fixture. No carrier confirmation/issued
        policy text exists yet at this stage, so this is always a fresh
        pre-bind READY/BLOCKED evaluation, same as a scenario's first pass."""
        return await self.run_live_update(ctx, broker_bind_instruction)

    async def run_live_update(
        self,
        ctx: Ctx,
        broker_bind_instruction: dict[str, Any],
        *,
        confirmation_raw_text: str | None = None,
        issued_policy_raw_text: str | None = None,
    ) -> OutputPackage:
        """Covers every live moment of this bind's lifecycle from the same
        ``broker_bind_instruction`` shape: initial creation (neither text —
        what ``run_live()`` above delegates to), attaching a real carrier
        bind confirmation once it arrives (confirmation text only), and
        attaching a real issued policy once THAT arrives (confirmation
        text must be re-supplied on this call too — re-pass whatever was
        previously persisted, e.g. via ``live_ingestion.
        load_live_bind_confirmation_text`` — or ``_decide_pre_bind_stage``
        will see ``self._parsed_confirmation is None`` and short-circuit
        back to ``READY``, per that function's own already-tested logic)."""
        self._bundle = ScenarioBundle(
            scenario_ref="live", broker_bind_instruction=broker_bind_instruction
        )
        self._submission_id = broker_bind_instruction.get("submission_id")
        self._as_of = datetime.now(UTC).date()

        documents = []
        if confirmation_raw_text is not None:
            documents.append(RawDocument(
                kind=DocumentKind.EMAIL, filename="carrier_bind_confirmation.txt",
                content=confirmation_raw_text,
            ))
        if issued_policy_raw_text is not None:
            documents.append(RawDocument(
                kind=DocumentKind.OTHER, filename="issued_policy_document_extract.txt",
                content=issued_policy_raw_text,
            ))

        raw = RawBundle(submission_id=self._submission_id, documents=documents)
        data = await self.extract(ctx, raw)
        decision = await self.decide(ctx, data)
        draft = await self.draft(ctx, decision)
        return await self.package(ctx, data, decision, draft)
