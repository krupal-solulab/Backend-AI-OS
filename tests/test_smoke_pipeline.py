"""End-to-end smoke test on the REAL Workflow_1 fixtures (Phase 1 DoD):
ingest(mock) → extract → rules → (stub decision) → review item → audit entry.
Plus the missing-document case (submission_09 → required-doc rule fails → missing-info).
"""

from __future__ import annotations

import pytest

from conftest import seed_and_publish_ruleset, stub_decide
from core.audit import DefaultAuditService
from core.common.dtos import AuditEntry, OutputPackage
from core.common.enums import DecisionOutcome, ReviewStatus, RuleCheckType
from core.documents import LocalDocumentStore
from core.extraction import DefaultExtractionService
from core.ingestion import MockConnectorService
from core.llm import build_llm_service
from core.review_queue import DefaultReviewQueueService

_KEY = "submission_validation"

pytestmark = pytest.mark.skipif(
    not __import__("core.config", fromlist=["get_settings"]).get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_1 fixtures unavailable",
)


async def test_end_to_end_happy_path(mem_session, mga_ctx) -> None:
    # 1. ingest (mock connector serves Workflow_1 fixtures)
    connector = MockConnectorService(workflow_n=1)
    raw = await connector.to_raw_bundle(mga_ctx, "submission_01")
    assert len(raw.documents) >= 4

    # 2. documents module: persist + retrieve
    store = LocalDocumentStore()
    for doc in raw.documents:
        await store.save(mem_session, mga_ctx, "submission_01", doc)
    stored = await store.list_for_submission(mem_session, mga_ctx, "submission_01")
    assert len(stored) == len(raw.documents)

    # 3. extract — cited field model
    model = await DefaultExtractionService().extract(mga_ctx, raw)
    assert any(f.name == "acord.named_insured" for f in model.fields)
    extracted = [f for f in model.fields if not f.name.startswith("documents.")]
    assert extracted and all(f.citation is not None for f in extracted)  # every value cited

    # 4. rules — all pass for submission_01
    engine = await seed_and_publish_ruleset(mem_session, mga_ctx, _KEY)
    results = await engine.evaluate(mem_session, mga_ctx, _KEY, model)
    assert results
    assert all(r.passed for r in results), [r.rule_id for r in results if not r.passed]

    # 5. stub decision (test harness only)
    decision = stub_decide(results)
    assert decision.outcome is DecisionOutcome.PROCEED

    # 6. draft via mock LLM (no API key needed) — grounded + cited
    llm = build_llm_service()
    draft = await llm.draft(mga_ctx, "Summarize this submission.", extracted[:5])
    assert draft.text
    assert all(c.filename for c in draft.citations)

    # 7. package → review queue → audit
    pkg = OutputPackage(
        submission_id="submission_01", decision=decision, draft=draft,
        payload={"premium": 18400},
    )
    rq = DefaultReviewQueueService()
    item = await rq.enqueue(mem_session, mga_ctx, pkg, "submission_triage")
    assert item.status is ReviewStatus.PENDING

    audit = DefaultAuditService()
    await audit.record(
        mem_session, mga_ctx,
        AuditEntry(
            actor="ai", who="system", what=f"decision={decision.outcome.value}",
            workflow="submission_triage", tenant_id=mga_ctx.tenant_id, vertical=mga_ctx.vertical,
        ),
    )
    entries = await audit.query(mem_session, mga_ctx, {"workflow": "submission_triage"})
    assert len(entries) == 1
    assert entries[0].actor == "ai"


async def test_missing_document_yields_request_info(mem_session, mga_ctx) -> None:
    # submission_09 has NO financial_statement (has an SOV instead)
    connector = MockConnectorService(workflow_n=1)
    raw = await connector.to_raw_bundle(mga_ctx, "submission_09")
    model = await DefaultExtractionService().extract(mga_ctx, raw)

    # extraction tolerated the variable doc set: no financials-present flag emitted
    assert not any(f.name == "documents.financials.present" for f in model.fields)
    assert any(f.name == "documents.sov.present" for f in model.fields)  # extra SOV present

    engine = await seed_and_publish_ruleset(mem_session, mga_ctx, _KEY)
    results = await engine.evaluate(mem_session, mga_ctx, _KEY, model)

    failed = {r.rule_id for r in results if not r.passed}
    assert "doc.financials.required" in failed

    decision = stub_decide(results)
    assert decision.outcome is DecisionOutcome.REQUEST_INFO

    missing_info = [
        r.message for r in results
        if not r.passed and r.check_type is RuleCheckType.REQUIRED
    ]
    assert "Financial statement is missing" in missing_info
