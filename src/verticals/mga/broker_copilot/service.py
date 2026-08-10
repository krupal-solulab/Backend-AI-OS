"""Broker Communication Copilot pipeline — read(existing Triage/Renewal OutputPackage) →
classify trigger → calibrate tone (Decision Core reuse) → LLM(grounded, cited) →
OutputPackage → review → audit. No new extraction/ingestion: every draft is generated
FROM a decision already produced by Submission Triage or Renewal Management (roadmap
"Reuses Extraction Core: High" / "Reuses Decision Core: Medium"). No auto-send.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, Citation, Ctx, Draft, ExtractedValue
from core.common.dtos import Decision as DecisionDTO
from core.common.dtos import OutputPackage as OutputPackageDTO
from core.common.enums import DecisionOutcome, DocumentKind, ReviewAction
from core.llm import build_llm_service
from core.models import Decision as DecisionRow
from core.models import OutputPackage as OutputPackageRow
from core.models import ReviewItem as ReviewItemRow
from core.review_queue import DefaultReviewQueueService
from verticals.mga.broker_copilot.drafting import BrokerDraftingEngine, BrokerRelationship
from verticals.mga.broker_copilot.schema import (
    ActivityEntry,
    BrokerContext,
    CommCitation,
    CommDraft,
)
from verticals.mga.models import MgaBrokerCommResult

WORKFLOW = "broker-copilot"

_SOURCE_WORKFLOWS = {
    "submission-triage": "Submission Triage",
    "renewal-management": "Renewal Management",
}
_SOURCE_ROUTE = {
    "submission-triage": "/app/workflows/submission-triage",
    "renewal-management": "/app/workflows/renewal-management",
}

_ACTIONS = {
    "approve": ReviewAction.APPROVE,
    "send": ReviewAction.SEND,       # human-triggered broker send (no auto-send)
    "escalate": ReviewAction.ESCALATE,
}


def _cite_str(c: Citation | None) -> str | None:
    if c is None:
        return None
    return f"{c.filename}:{c.locator}" if c.locator else c.filename


def _broker_from_detail(detail: dict[str, Any]) -> BrokerRelationship:
    """Every source workflow detail carries a ``broker`` block (Triage's narrative facts
    or Renewal's ``RenewalBroker``) — fall back to an unknown/neutral relationship if not."""
    b = detail.get("broker") or {}
    name = str(b.get("name") or "—")
    agency = str(b.get("agency") or "—")
    email = str(b.get("email") or "")
    tenure = b.get("tenureYears") if isinstance(b.get("tenureYears"), int) else None
    tier = b.get("volumeTier") if isinstance(b.get("volumeTier"), str) else None
    if not email and name != "—":
        local = re.sub(r"[^a-z.]+", "", name.lower().replace(" ", "."))
        email = f"{local}@{re.sub(r'[^a-z]+', '', agency.lower()) or 'broker'}.com"
    return BrokerRelationship(name=name, agency=agency, email=email or "broker@example.com",
                              tenure_years=tenure, volume_tier=tier)


class BrokerCopilotService:
    def __init__(self) -> None:
        self.engine = BrokerDraftingEngine()
        self.llm = build_llm_service()
        self.review_queue = DefaultReviewQueueService()
        self.audit = DefaultAuditService()

    async def _load_source(
        self, session: AsyncSession, ctx: Ctx, source_workflow: str, submission_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], DecisionOutcome, dict[str, Any]]:
        """Read back the source workflow's persisted OutputPackage + Decision.

        Returns (detail, row, outcome, decision_details) — ``row`` carries ``insured``
        for Triage (not present on ``TriageDetail`` itself); ``detail`` carries ``broker``
        for Renewal. Reading both keeps this workflow-shape-agnostic across the two
        sources without touching either workflow's schema.
        """
        pkg = (await session.execute(
            select(OutputPackageRow).where(
                col(OutputPackageRow.tenant_id) == ctx.tenant_id,
                col(OutputPackageRow.submission_id) == submission_id,
                col(OutputPackageRow.workflow) == source_workflow))
        ).scalars().first()
        if pkg is None or not pkg.payload:
            raise KeyError(f"no {source_workflow} output for submission '{submission_id}'")
        decision_row = (await session.execute(
            select(DecisionRow).where(
                col(DecisionRow.tenant_id) == ctx.tenant_id,
                col(DecisionRow.submission_id) == submission_id))
        ).scalars().first()
        outcome = decision_row.outcome if decision_row is not None else DecisionOutcome.PROCEED
        details = decision_row.details if decision_row is not None else {}
        return pkg.payload["detail"], pkg.payload.get("row", {}), outcome, details

    async def draft(
        self, session: AsyncSession, ctx: Ctx, source_workflow: str, submission_id: str,
    ) -> CommDraft:
        detail, row, outcome, details = await self._load_source(
            session, ctx, source_workflow, submission_id)

        comm_type = self.engine.classify(source_workflow, outcome, details)
        broker = _broker_from_detail(detail)
        plan = self.engine.calibrate_tone(comm_type, broker)
        named_insured = str(row.get("insured") or detail.get("broker", {}).get("name") or "—")
        subject = self.engine.subject_for(comm_type, named_insured)

        facts = self._facts(detail)
        llm_draft = await self.llm.draft(
            ctx, self.engine.instruction_for(comm_type, plan.tone), facts, tier="standard")
        citations = [CommCitation(claim=f.name, source=_cite_str(f.citation) or "—")
                     for f in facts if f.citation is not None]

        now = datetime.now(UTC).isoformat()
        draft_id = f"DRF-{submission_id[-6:] if len(submission_id) >= 6 else submission_id}"
        comm = CommDraft(
            id=draft_id, type=comm_type,
            sourceWorkflow=_SOURCE_WORKFLOWS[source_workflow], sourceId=submission_id,
            sourceRoute=_SOURCE_ROUTE[source_workflow], namedInsured=named_insured,
            broker=BrokerContext(name=broker.name, agency=broker.agency, email=broker.email,
                                 tenureYears=broker.tenure_years, volumeTier=broker.volume_tier),
            subject=subject, tone=plan.tone, toneWhy=plan.tone_why, sensitive=plan.sensitive,
            requiresComplianceReview=plan.requires_compliance_review, combined=plan.combined,
            deadlineRef=plan.deadline_ref, citations=citations, body=llm_draft.text,
            status="UNDER_COMPLIANCE_REVIEW" if plan.requires_compliance_review else "DRAFT",
            generatedAt=now,
            activity=[ActivityEntry(at=now, who="AI · Decision Core",
                                    what=f"Drafted {comm_type.replace('_', ' ').title()}",
                                    ctx=f"from {submission_id}", conf="—")],
        )

        session.add(MgaBrokerCommResult(
            tenant_id=ctx.tenant_id, submission_id=submission_id,
            source_workflow=source_workflow, comm_type=comm_type, tone=plan.tone,
            requires_compliance_review=plan.requires_compliance_review, sensitive=plan.sensitive))

        out_dto = OutputPackageDTO(
            submission_id=submission_id,
            decision=DecisionDTO(outcome=DecisionOutcome.PROCEED, score=None,
                                 rationale=f"Broker communication drafted: {comm_type}"),
            draft=Draft(text=comm.body, citations=llm_draft.citations),
            flags=[comm_type], missing_info=[],
            payload={"detail": comm.model_dump(by_alias=True)},
        )
        await self.review_queue.enqueue(session, ctx, out_dto, WORKFLOW)

        await self.audit.record(session, ctx, AuditEntry(
            actor="ai", who="system", what=f"Broker draft generated: {comm_type}",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"submission": submission_id, "type": comm_type}))
        await session.commit()
        return comm

    @staticmethod
    def _facts(detail: dict[str, Any]) -> list[ExtractedValue]:
        """Reassemble cited facts from the source detail's own narrative/citations —
        no re-extraction, per the roadmap's 'High Extraction Core reuse' rating."""
        facts: list[ExtractedValue] = []
        narrative = detail.get("narrative")
        if narrative:
            facts.append(ExtractedValue(name="source.narrative", value=narrative))
        for item in detail.get("missingInfo", []) or []:
            facts.append(ExtractedValue(
                name=f"missing.{item.get('item', 'item')}", value=item.get("reason", "")))
        for c in detail.get("consistency", []) or []:
            if c.get("status") in ("warn", "fail"):
                facts.append(ExtractedValue(name=f"consistency.{c.get('label', 'check')}",
                                            value=c.get("detail", "")))
        for f in detail.get("changes", []) or []:
            facts.append(ExtractedValue(name=f"change.{f.get('item', 'item')}",
                                        value=f.get("reason", ""),
                                        citation=Citation(document_kind=DocumentKind.OTHER,
                                                          filename=str(f.get("source") or "decision"))))
        return facts

    # ── list / detail / act ──
    async def list_drafts(self, session: AsyncSession, ctx: Ctx) -> list[CommDraft]:
        stmt = (select(ReviewItemRow, OutputPackageRow)
                .join(OutputPackageRow,
                      col(ReviewItemRow.output_package_id) == col(OutputPackageRow.id))
                .where(col(ReviewItemRow.tenant_id) == ctx.tenant_id,
                       col(ReviewItemRow.workflow) == WORKFLOW))
        drafts: list[CommDraft] = []
        for item, pkg in (await session.execute(stmt)).all():
            payload = pkg.payload or {}
            if "detail" not in payload:
                continue
            comm = CommDraft.model_validate(payload["detail"])
            comm.status = item.status.value if hasattr(item.status, "value") else str(item.status)
            drafts.append(comm)
        return drafts

    async def get_detail(
        self, session: AsyncSession, ctx: Ctx, draft_id: str
    ) -> CommDraft | None:
        for comm in await self.list_drafts(session, ctx):
            if comm.id == draft_id:
                return comm
        return None

    async def act(
        self, session: AsyncSession, ctx: Ctx, submission_id: str, action: str,
        body: str | None = None,
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
            raise KeyError(f"no broker-copilot review item for submission '{submission_id}'")
        result = await self.review_queue.act(session, ctx, item.id, review_action)
        await self.audit.record(session, ctx, AuditEntry(
            actor="human", who=ctx.user_id, what=f"{action} (role={ctx.role.value})",
            workflow=WORKFLOW, tenant_id=ctx.tenant_id, vertical=ctx.vertical,
            detail={"submission": submission_id, "action": action, "edited": body is not None}))
        await session.commit()
        return {"id": result.id, "status": result.status.value}
