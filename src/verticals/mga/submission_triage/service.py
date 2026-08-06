"""Submission Triage pipeline — wires the shared services + the MGA Appetite Engine.

ingest(mock) → extract → validate(shared RulesEngine) → decide(Appetite Engine) →
draft(grounded LLM, suppressed for DECLINE/manual per FR-23) → OutputPackage →
review-queue item → audit entry. Human actions (approve/send/escalate) go through the
shared review queue; nothing auto-sends.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.audit import DefaultAuditService
from core.common.dtos import (
    AuditEntry,
    Citation,
    Ctx,
    Draft,
    ExtractedModel,
    ExtractedValue,
)
from core.common.dtos import (
    OutputPackage as OutputPackageDTO,
)
from core.common.enums import DecisionOutcome, ReviewAction
from core.extraction import DefaultExtractionService
from core.extraction.service import coerce_number
from core.ingestion import build_connector_service
from core.llm import build_llm_service
from core.models import (
    Decision as DecisionRow,
)
from core.models import (
    OutputPackage as OutputPackageRow,
)
from core.models import (
    ReviewItem as ReviewItemRow,
)
from core.models import (
    Submission,
)
from core.review_queue import DefaultReviewQueueService
from verticals.mga.decision_core import AppetiteEngine
from verticals.mga.models import MgaAppetiteResult
from verticals.mga.rulesets import VALIDATION_KEY, ensure_ruleset, load_ruleset_json
from verticals.mga.submission_triage.schema import (
    ActivityEntry,
    AppetiteResultOut,
    ConsistencyCheck,
    ExtractedFieldOut,
    LossMetrics,
    MissingItem,
    RiskFactor,
    SubmissionRow,
    TriageDetail,
    TriageDoc,
    TriageMeta,
)

WORKFLOW = "submission-triage"
RULES_VERSION = f"{VALIDATION_KEY} v1"

_ACTIONS = {
    "approve": ReviewAction.APPROVE,
    "send": ReviewAction.SEND,       # human-triggered request-info to broker (no auto-send)
    "escalate": ReviewAction.ESCALATE,
}
# facts (with citations) handed to the grounded LLM narrative
_NARRATIVE_FIELDS = (
    "acord.named_insured", "acord.class_code", "acord.stated_annual_revenue",
    "acord.states_of_operation", "loss_run.total_incurred", "financials.total_revenue",
)
_APPETITE_LABEL = {
    DecisionOutcome.PROCEED: "In appetite",
    DecisionOutcome.REQUEST_INFO: "Needs info",
    DecisionOutcome.DECLINE: "Out of appetite",
}


def _fmt_money(value: Any) -> str:
    n = coerce_number(value)
    return f"${n:,.0f}" if n is not None else (str(value) if value not in (None, "") else "—")


def _humanize(leaf: str) -> str:
    return leaf.split(".", 1)[-1].replace("_", " ").title()


def _parse_years(period: Any) -> int:
    m = re.match(r"(\d+)", str(period or ""))
    return int(m.group(1)) if m else 0


def _cite_str(c: Citation | None) -> str | None:
    if c is None:
        return None
    return f"{c.filename}:{c.locator}" if c.locator else c.filename


class TriageService:
    def __init__(self, workflow_n: int = 1) -> None:
        self.workflow_n = workflow_n
        self.extraction = DefaultExtractionService()
        self.appetite = AppetiteEngine()
        self.llm = build_llm_service()
        self.review_queue = DefaultReviewQueueService()
        self.audit = DefaultAuditService()

    # ── run the pipeline for one submission ─────────
    async def triage(self, session: AsyncSession, ctx: Ctx, message_id: str) -> TriageDetail:
        sub = Submission(tenant_id=ctx.tenant_id, vertical=ctx.vertical,
                         external_ref=message_id, subject=message_id, status="triaged")
        session.add(sub)
        await session.flush()

        connector = build_connector_service(workflow_n=self.workflow_n)
        raw = await connector.to_raw_bundle(ctx, message_id)
        model = await self.extraction.extract(ctx, raw)

        engine = await ensure_ruleset(session, ctx)
        rule_results = await engine.evaluate(session, ctx, VALIDATION_KEY, model)
        decision = self.appetite.decide(model, rule_results)

        narrative, citations = await self._narrative(ctx, model, decision)
        detail = self._build_detail(model, raw, decision, narrative, citations)

        now = datetime.now(UTC).isoformat()
        detail.activity = [ActivityEntry(
            at=now, who="system (AI)",
            what=f"Auto-triaged -> {decision.outcome.value}",
            conf=f"{detail.confidence:.0%}")]
        row = self._build_row(sub.id, model, decision, detail)

        session.add(DecisionRow(
            tenant_id=ctx.tenant_id, submission_id=sub.id, outcome=decision.outcome,
            score=decision.score, rationale=decision.rationale, details=decision.details))
        session.add(MgaAppetiteResult(
            tenant_id=ctx.tenant_id, submission_id=sub.id, outcome=decision.outcome.value,
            score=decision.score, triggered_rule_ids=list(decision.details.get("failed_rules", [])),
            flags=list(decision.details.get("flags", []))))

        out_dto = OutputPackageDTO(
            submission_id=sub.id, decision=decision,
            draft=Draft(text=narrative, citations=[]),
            flags=list(decision.details.get("flags", [])),
            missing_info=[m["item"] for m in decision.details.get("missing_info", [])],
            payload={"detail": detail.model_dump(by_alias=True), "row": row.model_dump(),
                     "activity": [a.model_dump() for a in detail.activity]})
        await self.review_queue.enqueue(session, ctx, out_dto, WORKFLOW)

        await self.audit.record(session, ctx, AuditEntry(
            actor="ai", who="system", what=f"Triage recommendation: {decision.outcome.value}",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"submission": message_id, "score": decision.score}))
        await session.commit()
        return detail

    # ── narrative (grounded; suppressed for DECLINE / manual review) ──
    async def _narrative(
        self, ctx: Ctx, model: ExtractedModel, decision: Any
    ) -> tuple[str, list[str]]:
        if decision.details.get("suppress_narrative"):
            if decision.details.get("manual_review"):
                return ("Routed to manual review — extraction confidence below threshold; "
                        "automated triage suppressed on degraded input."), []
            return (f"Declined on hard rule(s): {decision.rationale} "
                    "Narrative suppressed pending underwriter confirmation."), []

        by_name = {f.name: f for f in model.fields}
        facts = [by_name[n] for n in _NARRATIVE_FIELDS if n in by_name]
        draft = await self.llm.draft(
            ctx, "Write a concise 2-3 sentence underwriting triage summary using only these facts.",
            facts, tier="standard")
        return draft.text, [s for c in draft.citations if (s := _cite_str(c))]

    # ── assemble the FE TriageDetail ────────────────
    def _build_detail(
        self, model: ExtractedModel, raw: Any, decision: Any,
        narrative: str, citations: list[str],
    ) -> TriageDetail:
        d = decision.details
        by_name: dict[str, ExtractedValue] = {f.name: f for f in model.fields}
        required_fields = self._required_fields()

        # docs
        present_kinds = [n.split(".")[1] for n in by_name if n.startswith("documents.")]
        docs: list[TriageDoc] = []
        for doc in raw.documents:
            kind = doc.kind.value
            doc_fields = [f for f in model.fields
                          if f.citation is not None and f.citation.filename == doc.filename]
            conf = min((f.confidence for f in doc_fields if f.confidence is not None), default=1.0)
            docs.append(TriageDoc(name=doc.filename, kind=kind, pages=1,
                                  fields=len(doc_fields), confidence=conf, classified=True))

        # extracted scalar fields
        fields_out: list[ExtractedFieldOut] = []
        for f in model.fields:
            if f.name.startswith("documents.") or isinstance(f.value, list):
                continue
            fields_out.append(ExtractedFieldOut(
                key=f.name, label=_humanize(f.name),
                value=None if f.value is None else str(f.value),
                required=f.name in required_fields,
                confidence=f.confidence if f.confidence is not None else 1.0,
                source=_cite_str(f.citation)))

        loss = LossMetrics(
            totalIncurred=_fmt_money(by_name.get("loss_run.total_incurred", _empty()).value),
            totalPaid=_fmt_money(by_name.get("loss_run.total_paid", _empty()).value),
            openClaims=int(coerce_number(by_name.get("loss_run.open_claims", _empty()).value) or 0),
            years=_parse_years(by_name.get("loss_run.total_incurred_period", _empty()).value),
            required=self.appetite.cfg.min_loss_years,
            trend=str(d.get("trend", "flat")))

        return TriageDetail(
            recommendation=decision.outcome.value,
            confidence=float(d.get("extraction_confidence", 1.0)),
            hardRulePassed=bool(d.get("hard_rule_passed", True)),
            failedRules=list(d.get("failed_rules", [])),
            processing="ready",
            rulesVersion=RULES_VERSION,
            meta=TriageMeta(received=sorted(present_kinds),
                            lowConfidence=list(d.get("low_confidence_fields", [])),
                            timestamp=datetime.now(UTC).isoformat()),
            docs=docs,
            fields=fields_out,
            loss=loss,
            consistency=[ConsistencyCheck(**c) for c in d.get("consistency", [])],
            missingInfo=[MissingItem(**m) for m in d.get("missing_info", [])],
            factors=[RiskFactor(name=f["name"], value=str(f["value"]), weight=int(f["weight"]))
                     for f in d.get("factors", [])],
            narrative=narrative,
            citations=citations,
            appetite=[AppetiteResultOut(rule=a["rule"], passed=a["pass"], hard=a["hard"],
                                        detail=a["detail"]) for a in d.get("appetite", [])],
            activity=[])

    def _build_row(
        self, sub_id: str, model: ExtractedModel, decision: Any, detail: TriageDetail
    ) -> SubmissionRow:
        by_name = {f.name: f.value for f in model.fields}
        class_code = str(by_name.get("acord.class_code") or "")
        industry = class_code.split(" - ", 1)[-1] if " - " in class_code else (class_code or "—")
        return SubmissionRow(
            id=sub_id,
            insured=str(by_name.get("acord.named_insured") or "—"),
            industry=industry,
            state=str(by_name.get("acord.states_of_operation") or "—"),
            tiv=_fmt_money(by_name.get("sov.total_insurable_value")),
            premium=_fmt_money(by_name.get("acord.prior_premium")),
            score=int(decision.score) if decision.score is not None else None,
            appetite=_APPETITE_LABEL[decision.outcome],
            recommendation=decision.outcome.value,
            status="pending",
            received=str(by_name.get("email.date") or detail.meta.timestamp))

    @staticmethod
    def _required_fields() -> set[str]:
        rs = load_ruleset_json("workflow1_validation")
        return {r["field"] for r in rs.get("rules", []) if r.get("check") == "required"}

    # ── list / detail / act ─────────────────────────
    async def list_rows(self, session: AsyncSession, ctx: Ctx) -> list[SubmissionRow]:
        stmt = (select(ReviewItemRow, OutputPackageRow)
                .join(OutputPackageRow,
                      col(ReviewItemRow.output_package_id) == col(OutputPackageRow.id))
                .where(col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                       col(ReviewItemRow.workflow) == WORKFLOW))
        rows: list[SubmissionRow] = []
        for item, pkg in (await session.execute(stmt)).all():
            payload = pkg.payload or {}
            row = SubmissionRow(**payload["row"])
            row.status = item.status.value if hasattr(item.status, "value") else str(item.status)
            rows.append(row)
        return rows

    async def get_detail(
        self, session: AsyncSession, ctx: Ctx, submission_id: str
    ) -> TriageDetail | None:
        pkg = (await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.submission_id) == submission_id,
                col(OutputPackageRow.workflow) == WORKFLOW))
        ).scalars().first()
        if pkg is None or not pkg.payload:
            return None
        return TriageDetail.model_validate(pkg.payload["detail"])

    async def act(
        self, session: AsyncSession, ctx: Ctx, submission_id: str, action: str,
        amount: float | None = None,
    ) -> dict[str, str]:
        review_action = _ACTIONS.get(action)
        if review_action is None:
            raise ValueError(f"unknown action '{action}'; allowed: {sorted(_ACTIONS)}")
        item = (await session.execute(
            select(ReviewItemRow).where(
                col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                col(ReviewItemRow.submission_id) == submission_id,
                col(ReviewItemRow.workflow) == WORKFLOW))
        ).scalars().first()
        if item is None:
            raise KeyError(f"no review item for submission '{submission_id}'")
        result = await self.review_queue.act(session, ctx, item.id, review_action, amount)
        await self.audit.record(session, ctx, AuditEntry(
            actor="human", who=ctx.user_id, what=f"{action} (role={ctx.role.value})",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"submission": submission_id, "action": action}))
        await session.commit()
        return {"id": result.id, "status": result.status.value}


def _empty() -> ExtractedValue:
    return ExtractedValue(name="_", value=None)
