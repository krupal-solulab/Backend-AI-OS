"""Contract (DTO) sanity checks — frozen in Phase 0."""

from __future__ import annotations

from core.common.dtos import RuleResult
from core.common.enums import RuleCheckType


def test_rule_result_score_defaults_to_none() -> None:
    r = RuleResult(rule_id="acord.required.fein", check_type=RuleCheckType.REQUIRED, passed=True)
    assert r.score is None


def test_rule_result_accepts_optional_graded_score() -> None:
    r = RuleResult(
        rule_id="es.match.carrier_a",
        check_type=RuleCheckType.COMPARE,
        passed=True,
        score=0.87,  # graded fit (e.g. E&S carrier match) alongside the boolean
    )
    assert r.score == 0.87
    assert r.passed is True
