"""Eval — Renewal Management against ALL 7 real Workflow_2 cases vs the PRD/README
expected-outcome spec, plus a synthesized missing-doc REQUEST_INFO case and a proof that
publishing a changed rule version changes the result (no code edit). Uses the loader;
never hardcodes fixtures.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select

import core.models  # noqa: F401
from core.common.dtos import Ctx, RawBundle, RawDocument
from core.common.enums import Role, RuleStatus, Vertical
from core.config import get_settings
from core.models import ReviewItem, RuleSet, RuleVersion, Tenant
from core.review_queue import AuthorityError
from fixtures import load_workflow
from verticals.mga.models import MgaRenewalResult
from verticals.mga.renewal_management.comparison import RenewalComparisonEngine
from verticals.mga.renewal_management.extraction import RenewalExtractionService
from verticals.mga.renewal_management.rulesets import VALIDATION_KEY, ensure_ruleset
from verticals.mga.renewal_management.service import WORKFLOW, RenewalService

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_2 fixtures unavailable",
)

EXPECTED = {
    "renewal_01": "RENEW_AS_IS",
    "renewal_02": "RENEW_WITH_CHANGES",
    "renewal_03": "RENEW_WITH_CHANGES",
    "renewal_04": "NON_RENEW",
    "renewal_05": "RENEW_AS_IS",
    "renewal_06": "RENEW_WITH_CHANGES",
    "renewal_07": "RENEW_AS_IS",
}


@pytest.fixture
async def mem_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(Tenant(id="demo-mga", name="Demo MGA", vertical=Vertical.MGA))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def mga_ctx() -> Ctx:
    return Ctx(tenant_id="demo-mga", vertical=Vertical.MGA, user_id="u-jr", role=Role.JUNIOR)


def _bundle(ref: str, *, drop: str | None = None) -> RawBundle:
    ls = {c.submission.external_ref: c for c in load_workflow(2)}[ref]
    docs = [RawDocument(kind=d.kind, filename=d.filename, content=d.content or "")
            for d in ls.documents if drop is None or drop not in d.filename]
    return RawBundle(submission_id=ref, documents=docs)


async def test_all_seven_recommendations_match_spec(mem_session, mga_ctx) -> None:
    svc = RenewalService()
    got = {ref: (await svc.renew(mem_session, mga_ctx, ref)).recommendation for ref in EXPECTED}
    assert got == EXPECTED, got


async def test_case_05_lapse_risk_but_renews(mem_session, mga_ctx) -> None:
    d = await RenewalService().renew(mem_session, mga_ctx, "renewal_05")
    assert d.recommendation != "NON_RENEW"
    assert d.timing.lapseRisk is True


async def test_case_03_loss_deterioration_flags(mem_session, mga_ctx) -> None:
    d = await RenewalService().renew(mem_session, mga_ctx, "renewal_03")
    assert d.recommendation == "RENEW_WITH_CHANGES"
    assert d.retention == "at-risk"
    assert any(f.category == "loss" for f in d.changeFlags)


async def test_case_04_non_renew_appetite_drift(mem_session, mga_ctx) -> None:
    d = await RenewalService().renew(mem_session, mga_ctx, "renewal_04")
    assert d.recommendation == "NON_RENEW"
    assert not d.hardRulePassed
    assert d.appetiteDrift and "exclude" in d.appetiteDrift.lower()


async def test_case_07_favorable_renew_as_is(mem_session, mga_ctx) -> None:
    d = await RenewalService().renew(mem_session, mga_ctx, "renewal_07")
    assert d.recommendation == "RENEW_AS_IS"
    assert d.retention == "favorable"
    assert any(lc.type == "favorable_closure" for lc in d.lossChanges)


async def test_premium_and_loss_ratio_surfaced(mem_session, mga_ctx) -> None:
    svc = RenewalService()
    d = await svc.renew(mem_session, mga_ctx, "renewal_03")
    assert d.priorPremium.startswith("$")   # from prior_policy.expiring_premium
    assert d.lossRatio.endswith("%")         # 5yr incurred / (prior premium × 5)
    assert d.indicated == "—" and d.premiumChange == "—"  # GAP: no re-rate computed
    assert any(c.label == "Premium" for c in d.comparison)  # also in the comparison table
    row = next(r for r in await svc.list_rows(mem_session, mga_ctx) if "Golden" in r.insured)
    assert row.priorPremium.startswith("$") and row.lossRatio.endswith("%")


async def test_synthesized_missing_doc_requests_info(mem_session, mga_ctx) -> None:
    # Drop the renewal questionnaire from a real case → completeness rule fails → REQUEST_INFO.
    engine = await ensure_ruleset(mem_session, mga_ctx)
    model = await RenewalExtractionService().extract(
        mga_ctx, _bundle("renewal_01", drop="renewal_questionnaire"))
    results = await engine.evaluate(mem_session, mga_ctx, VALIDATION_KEY, model)
    decision = RenewalComparisonEngine().decide(model, results)
    assert decision.outcome.value == "REQUEST_INFO"
    assert decision.details["needs_info"] is True
    assert decision.details["missing_info"]


async def test_publishing_new_rule_version_changes_result(mem_session, mga_ctx) -> None:
    """Author + publish a stricter rule version → a previously-clean case now needs info,
    with NO engine code change (proves rules are data)."""
    rules_engine = await ensure_ruleset(mem_session, mga_ctx)  # v1 published
    model = await RenewalExtractionService().extract(mga_ctx, _bundle("renewal_01"))
    r1 = RenewalComparisonEngine().decide(
        model, await rules_engine.evaluate(mem_session, mga_ctx, VALIDATION_KEY, model))
    assert r1.outcome.value != "REQUEST_INFO"  # clean under v1

    # v2 adds a required field renewal_01 does not provide (employee count on the questionnaire)
    rs = (await mem_session.execute(
        select(RuleSet).where(col(RuleSet.key) == VALIDATION_KEY))).scalar_one()
    v2_rules = {"category": "renewal_v2", "rules": [{
        "id": "questionnaire.employees.required",
        "field": "renewal_questionnaire.current_number_of_employees",
        "check": "required", "severity": "error",
        "message": "Employee count required on renewal questionnaire", "enabled": True}]}
    mem_session.add(RuleVersion(rule_set_id=rs.id, version=2, status=RuleStatus.DRAFT,
                                rules=v2_rules))
    await mem_session.commit()
    await rules_engine.publish(mem_session, mga_ctx, VALIDATION_KEY, 2)

    r2 = RenewalComparisonEngine().decide(
        model, await rules_engine.evaluate(mem_session, mga_ctx, VALIDATION_KEY, model))
    assert r2.outcome.value == "REQUEST_INFO"  # same case, new published rule → different result


async def test_persistence_and_rbac(mem_session, mga_ctx) -> None:
    svc = RenewalService()
    for ref in EXPECTED:
        await svc.renew(mem_session, mga_ctx, ref)
    rows = await svc.list_rows(mem_session, mga_ctx)
    assert len(rows) == 7
    assert (await mem_session.execute(select(MgaRenewalResult))).scalars().all().__len__() == 7

    detail = await svc.get_detail(mem_session, mga_ctx, rows[0].id)
    assert detail is not None

    item = (await mem_session.execute(
        select(ReviewItem).where(col(ReviewItem.workflow) == WORKFLOW))).scalars().first()
    assert item is not None and item.submission_id is not None
    with pytest.raises(AuthorityError):  # junior may not 'send' (senior-only)
        await svc.act(mem_session, mga_ctx, item.submission_id, "send")
