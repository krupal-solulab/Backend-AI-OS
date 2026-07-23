"""MGA rule sets (DATA authored in the Rules-Console JSON shape) + a helper to load
them into ``RuleSet`` → ``RuleVersion`` and publish. Rules are data, not code — this
module only ships the JSON and the loader, never inline logic."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.common.enums import RuleStatus
from core.models import RuleSet, RuleVersion
from core.rules_engine import DefaultRulesEngine

VALIDATION_KEY = "mga.submission_triage.validation"
_DIR = Path(__file__).parent


def load_ruleset_json(name: str) -> dict[str, Any]:
    """Load a rule-set JSON file shipped alongside this module."""
    return cast(dict[str, Any], json.loads((_DIR / f"{name}.json").read_text(encoding="utf-8")))


async def ensure_ruleset(
    session: AsyncSession, ctx: Ctx, key: str = VALIDATION_KEY,
    file: str = "workflow1_validation",
) -> DefaultRulesEngine:
    """Idempotently create + publish v1 of the rule set for this tenant. Returns the
    engine. Safe to call at eval setup or from a dev seed."""
    existing = (
        await session.execute(
            select(RuleSet).where(col(RuleSet.tenant_id) == ctx.tenant_id, col(RuleSet.key) == key)
        )
    ).scalar_one_or_none()
    engine = DefaultRulesEngine()
    if existing is not None and existing.active_version_id is not None:
        return engine

    rs = existing or RuleSet(tenant_id=ctx.tenant_id, vertical=ctx.vertical, key=key,
                             name="Submission Triage Validation")
    session.add(rs)
    await session.flush()
    rv = RuleVersion(rule_set_id=rs.id, version=1, status=RuleStatus.DRAFT,
                     rules=load_ruleset_json(file))
    session.add(rv)
    await session.commit()
    await engine.publish(session, ctx, key, 1)
    return engine
