"""E&S Carrier Appetite Intelligence Tracking eval test — proves the
pipeline + router against the REAL Workflow_18 dataset (originally
``Data sets/Workflow 9/carrier_intelligence_dataset``, copied to
``TEST_DATA_ROOT/Workflow_18/test_dataset`` per DATA_AND_FIXTURES.md).

Pytest-discovered here (not under src/verticals/es/...) — see
verticals/es/workflows/carrier_appetite_intelligence/eval_test.py for why.

Scenario 03 is the release-gate proof for this workflow: an
account-specific decline reason must never be scored like Scenario 02's
genuine class-level pattern. Scenario 01 proves single data points never
produce a suggestion regardless of consistency direction.
"""

from __future__ import annotations

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
from verticals.es.workflows.carrier_appetite_intelligence.router import (
    RunRequest,
    approve,
    dismiss,
    run_carrier_appetite_intelligence,
    run_carrier_appetite_intelligence_live,
)
from verticals.es.workflows.carrier_appetite_intelligence.service import (
    CarrierAppetiteIntelligencePipeline,
)

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_18 fixtures unavailable",
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


def _pipeline() -> CarrierAppetiteIntelligencePipeline:
    return CarrierAppetiteIntelligencePipeline(llm=build_llm_service())


async def _run(ctx: Ctx, scenario_ref: str):
    pipeline = _pipeline()
    return await pipeline.run(ctx, WorkflowInput(source_ref=scenario_ref))


async def test_scenario_01_single_data_point_suppressed(es_ctx) -> None:
    """A single, even fully-consistent, data point must never produce a
    suggestion — volume alone suppresses this."""
    output = await _run(es_ctx, "scenario_01")
    payload = output.payload
    assert payload["pattern_type"] == "INSUFFICIENT_SIGNAL"
    assert payload["status"] == "SUPPRESSED"
    assert payload["suggested_action"] is None
    assert payload["metadata_refresh"] is None
    assert output.decision.outcome is DecisionOutcome.PROCEED


async def test_scenario_02_genuine_pattern_suggestion(es_ctx) -> None:
    """3 of 4 recent outcomes inconsistent, 2 explicitly citing a
    class-level reason ("class no longer written") — a genuine pattern
    that must surface a human-reviewed suggestion."""
    output = await _run(es_ctx, "scenario_02")
    payload = output.payload
    assert payload["pattern_type"] == "GENUINE_INCONSISTENCY"
    assert payload["status"] == "PENDING_REVIEW"
    assert payload["suggested_action"]
    assert output.decision.outcome is DecisionOutcome.REQUEST_INFO

    reasons = {e["submission_id"]: e["reason_scope"] for e in payload["evidence"]}
    assert reasons["SUB-B1"] == "class_level"
    assert reasons["SUB-B2"] == "class_level"
    assert reasons["SUB-B3"] == "unstated"


async def test_scenario_03_account_specific_reason_suppressed(es_ctx) -> None:
    """RELEASE GATE: the single inconsistent outcome carries an explicit
    ACCOUNT-SPECIFIC reason ("severity exceeded ceiling for this specific
    account") — must contribute ZERO toward class-level evidence and
    never be scored like Scenario 02's genuine pattern."""
    output = await _run(es_ctx, "scenario_03")
    payload = output.payload
    assert payload["pattern_type"] == "INSUFFICIENT_SIGNAL"
    assert payload["status"] == "SUPPRESSED"
    assert payload["suggested_action"] is None

    reasons = {e["submission_id"]: e["reason_scope"] for e in payload["evidence"]}
    assert reasons["SUB-C2"] == "account_specific"


async def test_scenario_04_confirmed_consistent_metadata_refresh_only(es_ctx) -> None:
    """4 of 4 consistent — CI-03's one automatic write path: only
    appetite_confidence/appetite_last_updated are recorded as refreshed,
    never a substantive field, and no suggestion is generated."""
    output = await _run(es_ctx, "scenario_04")
    payload = output.payload
    assert payload["pattern_type"] == "CONFIRMED_CONSISTENT"
    assert payload["status"] == "METADATA_AUTO_UPDATED"
    assert payload["suggested_action"] is None
    assert payload["metadata_refresh"] is not None
    refresh_keys = {"appetite_confidence", "appetite_last_updated"}
    assert set(payload["metadata_refresh"].keys()) == refresh_keys
    assert output.decision.outcome is DecisionOutcome.PROCEED


async def test_run_and_approve(es_session) -> None:
    ctx = Ctx(tenant_id="demo-es", vertical=Vertical.ES, user_id="u-sr", role=Role.SENIOR)
    body = RunRequest(scenario_ref="scenario_02")
    item = await run_carrier_appetite_intelligence(body, ctx, es_session)
    approved = await approve(item.id, ctx, es_session)
    assert approved.status == "approved"


async def test_run_and_dismiss(es_ctx, es_session) -> None:
    body = RunRequest(scenario_ref="scenario_02")
    item = await run_carrier_appetite_intelligence(body, es_ctx, es_session)
    dismissed = await dismiss(item.id, es_ctx, es_session)
    assert dismissed.payload.status == "DISMISSED"


async def test_run_live_aggregates_real_quote_comparison_declinations(es_ctx, es_session) -> None:
    """The additive live-aggregation path (CI-01's real cross-workflow
    read): inserts real quote_comparison OutputPackage rows directly (same
    shape service.py's real pipeline produces), then confirms /run-live
    builds a genuine evaluation from them — never from the Workflow_18
    fixture. Also proves an "unable_to_determine" declination is excluded
    entirely, never guessed as consistent or inconsistent."""
    quotes_row_1 = OutputPackageRow(
        tenant_id=es_ctx.tenant_id,
        submission_id="SUB-LIVE-1",
        workflow="quote_comparison",
        payload={
            "submission_id": "SUB-LIVE-1",
            "quotes": [
                {
                    "quote_id": "Q-1",
                    "submission_id": "SUB-LIVE-1",
                    "carrier_name": "Live Test Carrier",
                    "response_type": "DECLINATION",
                    "declination_reason": "carrier no longer writes this class",
                    "declination_appetite_consistency": "inconsistent",
                },
                {
                    "quote_id": "Q-2",
                    "submission_id": "SUB-LIVE-1",
                    "carrier_name": "Live Test Carrier",
                    "response_type": "QUOTE",
                },
            ],
        },
    )
    quotes_row_2 = OutputPackageRow(
        tenant_id=es_ctx.tenant_id,
        submission_id="SUB-LIVE-2",
        workflow="quote_comparison",
        payload={
            "submission_id": "SUB-LIVE-2",
            "quotes": [
                {
                    "quote_id": "Q-3",
                    "submission_id": "SUB-LIVE-2",
                    "carrier_name": "Live Test Carrier",
                    "response_type": "DECLINATION",
                    "declination_reason": "appetite has changed for this class",
                    "declination_appetite_consistency": "inconsistent",
                },
                {
                    "quote_id": "Q-4",
                    "submission_id": "SUB-LIVE-2",
                    "carrier_name": "Live Test Carrier",
                    "response_type": "DECLINATION",
                    "declination_reason": "severity too high for this specific account",
                    "declination_appetite_consistency": "consistent",
                },
                {
                    "quote_id": "Q-5",
                    "submission_id": "SUB-LIVE-2",
                    "carrier_name": "Live Test Carrier",
                    "response_type": "DECLINATION",
                    "declination_reason": None,
                    "declination_appetite_consistency": "unable_to_determine",
                },
            ],
        },
    )
    es_session.add_all([quotes_row_1, quotes_row_2])
    await es_session.commit()

    items = await run_carrier_appetite_intelligence_live(es_ctx, es_session)

    assert len(items) == 1
    payload = items[0].payload
    assert payload.carrier_name == "Live Test Carrier"
    # 3 classifiable declinations feed the engine — the 4th ("unable_to_determine")
    # is excluded entirely, never defaulted to consistent/inconsistent.
    assert len(payload.evidence) == 3
    assert payload.pattern_type == "GENUINE_INCONSISTENCY"
    assert payload.status == "PENDING_REVIEW"
