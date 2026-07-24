"""Renewal rule sets (DATA, Rules-Console JSON) + loader. Rules are published to
``RuleSet`` → ``RuleVersion`` and resolved by PUBLISHED version at runtime — authoring a
new rule + publishing changes behavior with no code edit."""

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

VALIDATION_KEY = "mga.renewal_management.validation"
_DIR = Path(__file__).parent


def load_ruleset_json(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((_DIR / f"{name}.json").read_text(encoding="utf-8")))


async def ensure_ruleset(
    session: AsyncSession, ctx: Ctx, key: str = VALIDATION_KEY,
    file: str = "renewal_validation",
) -> DefaultRulesEngine:
    """Idempotently create + publish v1 of the renewal validation rule set."""
    existing = (
        await session.execute(
            select(RuleSet).where(col(RuleSet.tenant_id) == ctx.tenant_id, col(RuleSet.key) == key)
        )
    ).scalar_one_or_none()
    engine = DefaultRulesEngine()
    if existing is not None and existing.active_version_id is not None:
        return engine
    rs = existing or RuleSet(tenant_id=ctx.tenant_id, vertical=ctx.vertical, key=key,
                             name="Renewal Management Validation")
    session.add(rs)
    await session.flush()
    rv = RuleVersion(rule_set_id=rs.id, version=1, status=RuleStatus.DRAFT,
                     rules=load_ruleset_json(file))
    session.add(rv)
    await session.commit()
    await engine.publish(session, ctx, key, 1)
    return engine
