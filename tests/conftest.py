"""Shared test fixtures + helpers for the Phase 1 smoke tests."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.models  # noqa: F401  (registers tables)
from core.common.dtos import Ctx, Decision, RuleResult
from core.common.enums import DecisionOutcome, Role, RuleCheckType, RuleStatus, Vertical
from core.models import RuleSet, RuleVersion
from core.rules_engine import DefaultRulesEngine

_RULESET_PATH = Path(__file__).parent / "fixtures" / "ruleset_workflow1.json"


@pytest.fixture
async def mem_session() -> AsyncGenerator[AsyncSession, None]:
    """Isolated in-memory SQLite session with all tables + a demo MGA tenant."""
    from core.models import Tenant

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


def load_ruleset() -> dict:
    return json.loads(_RULESET_PATH.read_text(encoding="utf-8"))


async def seed_and_publish_ruleset(session: AsyncSession, ctx: Ctx, key: str) -> DefaultRulesEngine:
    """Load the smoke-test rule set into a RuleVersion and publish it. Returns the engine."""
    rs = RuleSet(tenant_id=ctx.tenant_id, vertical=ctx.vertical, key=key)
    session.add(rs)
    await session.flush()
    rv = RuleVersion(rule_set_id=rs.id, version=1, status=RuleStatus.DRAFT, rules=load_ruleset())
    session.add(rv)
    await session.commit()
    engine = DefaultRulesEngine()
    await engine.publish(session, ctx, key, 1)
    return engine


def stub_decide(results: list[RuleResult]) -> Decision:
    """TEST-HARNESS-ONLY stub decision (real decision core is Phase 2, per vertical):
    any failed REQUIRED rule → REQUEST_INFO (missing info), else PROCEED."""
    failed_required = [
        r for r in results if not r.passed and r.check_type is RuleCheckType.REQUIRED
    ]
    outcome = DecisionOutcome.REQUEST_INFO if failed_required else DecisionOutcome.PROCEED
    return Decision(outcome=outcome, rule_results=results)
