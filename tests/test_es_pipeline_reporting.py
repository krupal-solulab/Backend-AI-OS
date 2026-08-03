"""E&S Pipeline & Carrier Performance Reporting eval test — proves the
pipeline + router against the REAL Workflow_19 dataset (originally
``Data sets/Workflow 10/pipeline_reporting_dataset``, copied to
``TEST_DATA_ROOT/Workflow_19/test_dataset`` per DATA_AND_FIXTURES.md).

Pytest-discovered here (not under src/verticals/es/...) — see
verticals/es/workflows/pipeline_reporting/eval_test.py for why.

Scenario 03 is the release-gate proof for this workflow: a logging gap
must never be silently interpolated or omitted. Scenario 02 proves a
low-volume carrier figure is never ranked/presented as equally reliable.
Scenario 04 proves a $0-savings confirmation never reads as a failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.common.dtos import Ctx, WorkflowInput
from core.common.enums import DecisionOutcome, Role, Vertical
from core.config import get_settings
from core.llm import build_llm_service
from core.models import OutputPackage as OutputPackageRow
from core.models import Tenant
from verticals.es.workflows.pipeline_reporting.router import (
    RunRequest,
    run_pipeline_reporting,
    run_pipeline_reporting_live,
)
from verticals.es.workflows.pipeline_reporting.service import PipelineReportingPipeline

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_19 fixtures unavailable",
)


@pytest.fixture
def es_ctx() -> Ctx:
    return Ctx(tenant_id="demo-es", vertical=Vertical.ES, user_id="u-jr", role=Role.JUNIOR)


@pytest.fixture
async def es_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(Tenant(id="demo-es", name="Demo E&S", vertical=Vertical.ES))
        await session.commit()
        yield session
    await engine.dispose()


def _pipeline() -> PipelineReportingPipeline:
    return PipelineReportingPipeline(llm=build_llm_service())


async def _run(ctx: Ctx, scenario_ref: str):
    pipeline = _pipeline()
    return await pipeline.run(ctx, WorkflowInput(source_ref=scenario_ref))


async def test_scenario_01_clean_funnel_baseline(es_ctx) -> None:
    output = await _run(es_ctx, "scenario_01")
    payload = output.payload
    assert payload["data_completeness"]["status"] == "COMPLETE"
    assert output.decision.outcome is DecisionOutcome.PROCEED
    stages = {s["stage"]: s for s in payload["funnel"]}
    assert stages["Bound"]["count"] == 47
    assert stages["Bound"]["pct_of_prior_stage"] == 81.0
    # 47/84 = 55.952...% -> rounds to 56.0 (the dataset's own prose says "55.9%",
    # a minor hand-authoring imprecision — illustrative text, not a literal
    # assertion target, same precedent as every prior workflow's expected_output).
    assert payload["overall_conversion_pct"] == 56.0


async def test_scenario_02_low_volume_carrier_annotated_and_ordered(es_ctx) -> None:
    output = await _run(es_ctx, "scenario_02")
    payload = output.payload
    carriers = payload["carrier_performance"]
    # Ordered by volume, NOT by hit-rate — Ironclad (22) must come before
    # Vantage (4) even though Vantage's raw rate is higher.
    assert [c["carrier_name"] for c in carriers] == [
        "Ironclad Casualty Solutions", "Vantage Excess Partners",
    ]
    ironclad, vantage = carriers
    assert ironclad["overall_hit_rate"] == 63.6
    assert ironclad["low_volume_flag"] is False
    assert vantage["overall_hit_rate"] == 100.0
    assert vantage["low_volume_flag"] is True


async def test_scenario_03_data_gap_never_interpolated(es_ctx) -> None:
    """RELEASE GATE: the gapped stage's own figure AND the following
    stage's percentage must both be explicitly withheld — never
    estimated — while the following stage's raw count still shows since
    its own logging is unaffected."""
    output = await _run(es_ctx, "scenario_03")
    payload = output.payload
    assert payload["data_completeness"]["status"] == "PARTIAL"
    assert output.decision.outcome is DecisionOutcome.REQUEST_INFO
    gaps = payload["data_completeness"]["gaps"]
    assert any(g["stage"] == "Compared & Selected" for g in gaps)

    stages = {s["stage"]: s for s in payload["funnel"]}
    assert stages["Compared & Selected"]["count"] is None
    assert stages["Compared & Selected"]["pct_of_prior_stage"] is None
    assert stages["Bound"]["count"] == 47  # bind logging itself unaffected
    assert stages["Bound"]["pct_of_prior_stage"] is None  # denominator unknown

    # Never silently smoothed into a clean top-line figure either.
    assert payload["overall_conversion_pct"] is None


async def test_scenario_04_confirmation_value_not_a_failure(es_ctx) -> None:
    output = await _run(es_ctx, "scenario_04")
    payload = output.payload
    outcomes = {o["account"]: o for o in payload["remarketing_value"]}

    summit = outcomes["Summit Roofing Group"]
    assert summit["outcome_type"] == "confirmation_value"
    assert summit["savings_amount"] is None

    clearpath = outcomes["Clearpath Bookkeeping (prior 2 cycles)"]
    assert clearpath["outcome_type"] == "not_remarketed"
    assert clearpath["savings_amount"] is None


async def test_run_endpoint(es_ctx, es_session) -> None:
    body = RunRequest(scenario_ref="scenario_01")
    item = await run_pipeline_reporting(body, es_ctx, es_session)
    assert item.status == "pending"
    assert item.payload.period == "Q3 2027"


async def test_run_live_aggregates_real_cross_workflow_rows(es_ctx, es_session) -> None:
    """The additive live-aggregation path (PR-01's real cross-workflow
    read): inserts real OutputPackage rows directly (same shape each real
    workflow's own service.py produces) across market_matching,
    package_assembly, quote_comparison, binder_issuance, and
    renewal_remarketing, then confirms /run-live builds a genuine report
    from them — never from the Workflow_19 fixture."""
    rows = [
        OutputPackageRow(
            tenant_id=es_ctx.tenant_id, submission_id="SUB-P1", workflow="market_matching",
            payload={
                "submission_id": "SUB-P1",
                "matches": [{"carrier_id": "CAR-IC", "carrier_name": "Ironclad", "score": 82.0}],
                "excluded": [], "diligent_search": {
                    "required": False, "on_file": 0, "compliant": True, "note": "n/a",
                },
            },
        ),
        OutputPackageRow(
            tenant_id=es_ctx.tenant_id, submission_id="SUB-P2", workflow="market_matching",
            payload={
                "submission_id": "SUB-P2", "matches": [], "excluded": [],
                "diligent_search": {
                    "required": False, "on_file": 0, "compliant": True, "note": "n/a",
                },
            },
        ),
        OutputPackageRow(
            tenant_id=es_ctx.tenant_id, submission_id="SUB-P1", workflow="package_assembly",
            payload={
                "package_id": "PKG-1", "submission_id": "SUB-P1", "carrier_id": "CAR-IC",
                "carrier_name": "Ironclad", "status": "READY",
                "cover_letter": {"body": "...", "citations": []},
            },
        ),
        OutputPackageRow(
            tenant_id=es_ctx.tenant_id, submission_id="SUB-P1", workflow="package_assembly",
            payload={
                "package_id": "PKG-2", "submission_id": "SUB-P1", "carrier_id": "CAR-VT",
                "carrier_name": "Vantage", "status": "READY",
                "cover_letter": {"body": "...", "citations": []},
            },
        ),
        OutputPackageRow(
            tenant_id=es_ctx.tenant_id, submission_id="SUB-P1", workflow="quote_comparison",
            payload={
                "submission_id": "SUB-P1",
                "quotes": [
                    {
                        "quote_id": "Q-IC", "submission_id": "SUB-P1", "carrier_name": "Ironclad",
                        "response_type": "QUOTE",
                    },
                    {
                        "quote_id": "Q-VT", "submission_id": "SUB-P1", "carrier_name": "Vantage",
                        "response_type": "DECLINATION",
                    },
                ],
                "comparability_assessment": {"directly_comparable": True, "material_differences": []},
                "output_mode": "SINGLE_RECOMMENDATION",
                "recommendation": {"reasoning": {"summary": "..."}},
                "selected_quote_id": "Q-IC",
            },
        ),
        OutputPackageRow(
            tenant_id=es_ctx.tenant_id, submission_id="SUB-P1", workflow="binder_issuance",
            payload={
                "bind_id": "BIND-1", "submission_id": "SUB-P1", "carrier_id": "CAR-IC",
                "carrier_name": "Ironclad", "requested_bind_terms": {},
                "carrier_confirmation": {"binder_number": "BINDER-123"},
            },
        ),
        OutputPackageRow(
            tenant_id=es_ctx.tenant_id, submission_id="RR-1", workflow="renewal_remarketing",
            payload={
                "renewal_review_id": "RR-1", "named_insured": "Some Account",
                "is_comparison_stage": False,
                "trigger_decision": {
                    "level": "FULL_REMARKET",
                    "reasoning": {"summary": "Adverse pricing vs. exposure change."},
                },
            },
        ),
    ]
    es_session.add_all(rows)
    await es_session.commit()

    item = await run_pipeline_reporting_live(es_ctx, es_session)
    payload = item.payload

    stages = {s.stage: s for s in payload.funnel}
    assert stages["Submissions Received"].count == 2
    assert stages["Matched to Carrier"].count == 1
    assert stages["Packages Assembled"].count == 1
    assert stages["Quotes Received"].count == 1
    assert stages["Compared & Selected"].count == 1
    assert stages["Bound"].count == 1
    assert payload.data_completeness.status == "COMPLETE"

    carriers = {c.carrier_name: c for c in payload.carrier_performance}
    assert carriers["Ironclad"].submissions_approached == 1
    assert carriers["Ironclad"].quote_rate == 100.0
    assert carriers["Ironclad"].overall_hit_rate == 100.0
    assert carriers["Ironclad"].low_volume_flag is True  # 1 < the placeholder threshold
    assert carriers["Vantage"].quote_rate == 0.0

    outcome = payload.remarketing_value[0]
    assert outcome.account == "Some Account"
    # No workflow anywhere records a real carrier-switch decision yet, so a
    # remarket that was triggered but never confirmed switched honestly
    # produces confirmation_value, never a guessed savings figure.
    assert outcome.outcome_type == "confirmation_value"
    assert outcome.savings_amount is None


async def test_run_live_time_to_placement_is_raw_elapsed_days(es_ctx, es_session) -> None:
    """Gap-fill (PR-03/FR-4): real elapsed time from a submission's
    earliest Market Matching row to its Binder Issuance bind, per carrier
    — joined by submission_id, the same real join key funnel counting
    already relies on. FR-4's broker/agent-delay exclusion is honestly
    NOT applied (Package Assembly has no history of BLOCKED-status
    duration to compute it from) — this proves the raw elapsed-day math
    itself is correct and grounded in real timestamps, never fabricated."""
    matched_at = datetime(2027, 6, 1, 12, 0, tzinfo=UTC)
    bound_at = matched_at + timedelta(days=14)

    rows = [
        OutputPackageRow(
            tenant_id=es_ctx.tenant_id, submission_id="SUB-T1", workflow="market_matching",
            created_at=matched_at,
            payload={
                "submission_id": "SUB-T1",
                "matches": [{"carrier_id": "CAR-IC", "carrier_name": "Ironclad", "score": 82.0}],
                "excluded": [],
                "diligent_search": {"required": False, "on_file": 0, "compliant": True, "note": "n/a"},
            },
        ),
        OutputPackageRow(
            tenant_id=es_ctx.tenant_id, submission_id="SUB-T1", workflow="binder_issuance",
            created_at=bound_at,
            payload={
                "bind_id": "BIND-T1", "submission_id": "SUB-T1", "carrier_id": "CAR-IC",
                "carrier_name": "Ironclad", "requested_bind_terms": {},
                "carrier_confirmation": {"binder_number": "BINDER-T1"},
            },
        ),
    ]
    es_session.add_all(rows)
    await es_session.commit()

    item = await run_pipeline_reporting_live(es_ctx, es_session)
    payload = item.payload

    placement = next(p for p in payload.time_to_placement if p.carrier_name == "Ironclad")
    assert placement.submissions_bound == 1
    assert placement.avg_days == 14.0
    assert placement.delay_excluded is False  # honest — FR-4's exclusion is not computed
