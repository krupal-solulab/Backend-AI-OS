"""Builds + publishes one ``RuleSet``/``RuleVersion`` PER CARRIER through the
shared ``core.rules_engine`` — the "carrier-appetite rule sets as data" pattern
CORE_MODULES.md describes. Rule JSON matches the engine's REAL, current shape
(``params.value`` for min/max; plain ``required`` for doc-presence flags) —
see matching.py's module docstring for why this isn't the originally-drafted
``params.min``/``params.max``/``category``+``severity`` shape.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from core.common.dtos import Ctx
from core.common.enums import RuleStatus
from core.models import RuleSet, RuleVersion
from core.rules_engine import DefaultRulesEngine
from verticals.es.decision_core.carrier_profiles import CarrierProfile
from verticals.es.decision_core.matching import ruleset_key

_DOC_REQUIREMENT_PATTERNS: list[tuple[str, str, str]] = [
    # (substring to match in the requirement string, doc-kind, human label)
    ("acord", "acord", "ACORD application"),
    ("loss run", "loss_run", "Loss run"),
    ("financial", "financials", "Current financials"),
    ("statement of values", "sov", "Statement of Values (SOV)"),
    ("sov", "sov", "Statement of Values (SOV)"),
]


def _doc_requirement_rule(requirement: str, index: int) -> dict[str, Any]:
    """Maps one `required_documents` string to a `required` check. Requirements
    with no corresponding extraction doc-kind (e.g. a supplemental
    questionnaire) reference a field name this dataset never populates —
    honestly modeling "we have no way to satisfy this yet", not silently
    dropped."""
    low = requirement.lower()
    for substr, kind, _label in _DOC_REQUIREMENT_PATTERNS:
        if substr in low:
            return {
                "id": f"doc.{kind}.required",
                "label": requirement,
                "field": f"documents.{kind}.present",
                "check": "required",
                "severity": "warn",
                "message": f"{requirement} is missing",
                "enabled": True,
            }
    slug = re.sub(r"[^a-z0-9]+", "_", low).strip("_")
    return {
        "id": f"doc.unmapped.{index}.{slug}",
        "label": requirement,
        "field": f"documents.unmapped_{slug}.present",  # never populated by extraction
        "check": "required",
        "severity": "warn",
        "message": f"{requirement} is required but this document type isn't captured yet",
        "enabled": True,
    }


def build_carrier_ruleset(profile: CarrierProfile) -> dict[str, Any]:
    """The Rules-Console JSON for one carrier: premium band (MM-03 input) +
    loss-run-years / required-documents (MM-06 completeness input)."""
    band = profile.premium_band
    sr = profile.submission_requirements

    rules: list[dict[str, Any]] = [
        {
            "id": "premium.min",
            "label": f"Premium >= {profile.carrier_name}'s floor",
            "field": "acord.indicated_premium_target",
            "check": "min",
            "params": {"value": band.min},
            "severity": "error",
            "message": f"Indicated premium below {profile.carrier_name}'s appetite floor "
            f"(${band.min:,.0f})",
            "enabled": True,
        },
        {
            "id": "premium.max",
            "label": f"Premium <= {profile.carrier_name}'s ceiling",
            "field": "acord.indicated_premium_target",
            "check": "max",
            "params": {"value": band.max},
            "severity": "error",
            "message": f"Indicated premium above {profile.carrier_name}'s appetite ceiling "
            f"(${band.max:,.0f})",
            "enabled": True,
        },
        {
            "id": "loss_run.years.min",
            "label": f"Loss run covers >= {sr.min_loss_run_years} years",
            "field": "loss_run.years_of_history_provided",
            "check": "min",
            "params": {"value": sr.min_loss_run_years},
            "severity": "warn",
            "message": f"{profile.carrier_name} requires {sr.min_loss_run_years}-year loss run "
            "history",
            "enabled": True,
        },
    ]
    for i, requirement in enumerate(sr.required_documents):
        rules.append(_doc_requirement_rule(requirement, i))

    return {"category": "es_carrier_appetite", "carrier_id": profile.carrier_id, "rules": rules}


async def seed_and_publish_carrier_rulesets(
    session: AsyncSession, ctx: Ctx, panel: list[CarrierProfile]
) -> DefaultRulesEngine:
    """Idempotent-ish: creates version 1 + publishes for each carrier not already
    seeded for this tenant. Returns the engine ready to `.evaluate(...)`."""
    engine = DefaultRulesEngine()
    for profile in panel:
        key = ruleset_key(profile.carrier_id)
        existing = (
            await session.execute(
                select(RuleSet).where(
                    col(RuleSet.tenant_id) == ctx.tenant_id, col(RuleSet.key) == key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        rs = RuleSet(
            tenant_id=ctx.tenant_id, vertical=ctx.vertical, key=key,
            name=f"{profile.carrier_name} appetite",
        )
        session.add(rs)
        await session.flush()
        rv = RuleVersion(
            rule_set_id=rs.id, version=1, status=RuleStatus.DRAFT,
            rules=build_carrier_ruleset(profile),
        )
        session.add(rv)
        await session.commit()
        await engine.publish(session, ctx, key, 1)
    return engine


SUBMISSION_VALIDATION_KEY = "es_market_matching_submission_validation"

_SUBMISSION_VALIDATION_RULESET: dict[str, Any] = {
    "category": "es_market_matching_submission_validation",
    "rules": [
        {
            "id": "doc.acord.required",
            "label": "ACORD application present",
            "field": "documents.acord.present",
            "check": "required",
            "severity": "error",
            "message": "ACORD application is missing — cannot determine class code or "
            "indicated premium",
            "enabled": True,
        }
    ],
}


async def seed_and_publish_submission_validation_ruleset(
    session: AsyncSession, ctx: Ctx
) -> DefaultRulesEngine:
    """Baseline, submission-level validation — separate from per-carrier appetite
    matching. If the ACORD itself is missing, there's no class code / premium to
    match against ANY carrier, which is a genuine REQUEST_INFO case independent
    of carrier fit (unlike a per-carrier missing document, which only scores/
    informs per MM-06 — see matching.py)."""
    engine = DefaultRulesEngine()
    existing = (
        await session.execute(
            select(RuleSet).where(
                col(RuleSet.tenant_id) == ctx.tenant_id,
                col(RuleSet.key) == SUBMISSION_VALIDATION_KEY,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        rs = RuleSet(
            tenant_id=ctx.tenant_id, vertical=ctx.vertical, key=SUBMISSION_VALIDATION_KEY,
            name="Market Matching submission validation",
        )
        session.add(rs)
        await session.flush()
        rv = RuleVersion(
            rule_set_id=rs.id, version=1, status=RuleStatus.DRAFT,
            rules=_SUBMISSION_VALIDATION_RULESET,
        )
        session.add(rv)
        await session.commit()
        await engine.publish(session, ctx, SUBMISSION_VALIDATION_KEY, 1)
    return engine
