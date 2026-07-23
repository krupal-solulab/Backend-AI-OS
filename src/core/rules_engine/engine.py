"""The generic evaluator + versioning (publish/rollback).

The evaluator is a pure function over an ``ExtractedModel``; the DB-backed
``DefaultRulesEngine`` resolves the active published ``RuleVersion`` for a rule-set key
and delegates to it. (This module's interface — session-taking methods — is a Phase-1
implementation detail; the FROZEN contract is only ``RuleResult`` in ``core.common``.)
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Citation, ExtractedModel, RuleResult
from core.common.enums import RuleCheckType, RuleStatus
from core.models import RuleSet, RuleVersion

_COMPARATORS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


def _to_number(value: Any) -> float | None:
    """Coerce a currency/percent/plain string to float, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if value is None:
        return None
    cleaned = re.sub(r"[,$%\s]", "", str(value))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _flatten(model: ExtractedModel) -> tuple[dict[str, Any], dict[str, Citation]]:
    data: dict[str, Any] = {}
    citations: dict[str, Citation] = {}
    for fv in model.fields:
        data[fv.name] = fv.value
        if fv.citation is not None:
            citations[fv.name] = fv.citation
    return data, citations


def _eval_one(rule: dict[str, Any], data: dict[str, Any]) -> tuple[bool, RuleCheckType]:
    check = RuleCheckType(rule["check"])
    field = rule.get("field", "")
    params = rule.get("params") or {}
    value = data.get(field)

    if check is RuleCheckType.REQUIRED:
        return _present(value), check

    if check is RuleCheckType.REGEX:
        if not _present(value):
            return False, check
        return bool(re.search(str(params.get("pattern", "")), str(value))), check

    if check is RuleCheckType.MIN:
        n, lo = _to_number(value), _to_number(params.get("value"))
        return (n is not None and lo is not None and n >= lo), check

    if check is RuleCheckType.MAX:
        n, hi = _to_number(value), _to_number(params.get("value"))
        return (n is not None and hi is not None and n <= hi), check

    if check in (RuleCheckType.COMPARE, RuleCheckType.CROSS_DOC):
        op = params.get("op", "==")
        comparator = _COMPARATORS.get(op)
        if comparator is None:
            return False, check
        # Right-hand side is another field ("field") or a literal ("value").
        if "field" in params:
            other = data.get(params["field"])
            if not _present(value) or not _present(other):
                return False, check
            left_n, right_n = _to_number(value), _to_number(other)
            if left_n is not None and right_n is not None:
                return comparator(left_n, right_n), check
            return comparator(str(value).strip().lower(), str(other).strip().lower()), check
        rhs = params.get("value")
        left_n, right_n = _to_number(value), _to_number(rhs)
        if left_n is not None and right_n is not None:
            return comparator(left_n, right_n), check
        return comparator(value, rhs), check

    return False, check


def evaluate_ruleset(ruleset: dict[str, Any], model: ExtractedModel) -> list[RuleResult]:
    """Pure evaluation of a rule-set JSON over an ExtractedModel. Disabled rules are
    skipped. Each result carries the source citation for its field when available."""
    data, citations = _flatten(model)
    results: list[RuleResult] = []
    for rule in ruleset.get("rules", []):
        if not rule.get("enabled", True):
            continue
        passed, check = _eval_one(rule, data)
        results.append(
            RuleResult(
                rule_id=rule["id"],
                check_type=check,
                passed=passed,
                message=None if passed else rule.get("message", rule.get("label")),
                citation=citations.get(rule.get("field", "")),
            )
        )
    return results


class RulesEngine(Protocol):
    async def evaluate(
        self, session: AsyncSession, ctx: Any, rule_set_key: str, model: ExtractedModel
    ) -> list[RuleResult]: ...
    async def publish(
        self, session: AsyncSession, ctx: Any, rule_set_key: str, version: int
    ) -> None: ...
    async def rollback(
        self, session: AsyncSession, ctx: Any, rule_set_key: str, version: int
    ) -> None: ...


class DefaultRulesEngine:
    """DB-backed engine: resolves the active published RuleVersion, then evaluates."""

    async def _get_ruleset(
        self, session: AsyncSession, tenant_id: str, rule_set_key: str
    ) -> tuple[RuleSet, RuleVersion] | None:
        rs = (
            await session.execute(
                select(RuleSet).where(
                    col(RuleSet.tenant_id) == tenant_id, col(RuleSet.key) == rule_set_key
                )
            )
        ).scalar_one_or_none()
        if rs is None or rs.active_version_id is None:
            return None
        rv = (
            await session.execute(
                select(RuleVersion).where(col(RuleVersion.id) == rs.active_version_id)
            )
        ).scalar_one_or_none()
        if rv is None:
            return None
        return rs, rv

    async def evaluate(
        self, session: AsyncSession, ctx: Any, rule_set_key: str, model: ExtractedModel
    ) -> list[RuleResult]:
        found = await self._get_ruleset(session, ctx.tenant_id, rule_set_key)
        if found is None:
            return []
        _, rv = found
        return evaluate_ruleset(rv.rules or {}, model)

    async def publish(
        self, session: AsyncSession, ctx: Any, rule_set_key: str, version: int
    ) -> None:
        found = await self._get_version(session, ctx.tenant_id, rule_set_key, version)
        if found is None:
            raise ValueError(f"no version {version} for rule set '{rule_set_key}'")
        rs, rv = found
        rv.status = RuleStatus.PUBLISHED
        rs.active_version_id = rv.id
        session.add(rv)
        session.add(rs)
        await session.commit()

    async def rollback(
        self, session: AsyncSession, ctx: Any, rule_set_key: str, version: int
    ) -> None:
        """Roll the active pointer back to ``version`` and mark the current one rolled back."""
        found = await self._get_version(session, ctx.tenant_id, rule_set_key, version)
        if found is None:
            raise ValueError(f"no version {version} for rule set '{rule_set_key}'")
        rs, target = found
        if rs.active_version_id and rs.active_version_id != target.id:
            current = (
                await session.execute(
                    select(RuleVersion).where(col(RuleVersion.id) == rs.active_version_id)
                )
            ).scalar_one_or_none()
            if current is not None:
                current.status = RuleStatus.ROLLED_BACK
                session.add(current)
        target.status = RuleStatus.PUBLISHED
        rs.active_version_id = target.id
        session.add(target)
        session.add(rs)
        await session.commit()

    async def _get_version(
        self, session: AsyncSession, tenant_id: str, rule_set_key: str, version: int
    ) -> tuple[RuleSet, RuleVersion] | None:
        rs = (
            await session.execute(
                select(RuleSet).where(
                    col(RuleSet.tenant_id) == tenant_id, col(RuleSet.key) == rule_set_key
                )
            )
        ).scalar_one_or_none()
        if rs is None:
            return None
        rv = (
            await session.execute(
                select(RuleVersion).where(
                    col(RuleVersion.rule_set_id) == rs.id, col(RuleVersion.version) == version
                )
            )
        ).scalar_one_or_none()
        if rv is None:
            return None
        return rs, rv
