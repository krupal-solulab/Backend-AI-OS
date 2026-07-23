"""Rules engine — all 6 check types (pass + fail) and publish/rollback versioning."""

from __future__ import annotations

from conftest import load_ruleset
from core.common.dtos import Citation, ExtractedModel, ExtractedValue
from core.common.enums import DocumentKind, RuleStatus
from core.models import RuleSet, RuleVersion
from core.rules_engine import DefaultRulesEngine, evaluate_ruleset


def _val(name: str, value: object) -> ExtractedValue:
    return ExtractedValue(
        name=name, value=value,
        citation=Citation(document_kind=DocumentKind.ACORD, filename="acord_application.txt"),
    )


def _model(**pairs: object) -> ExtractedModel:
    return ExtractedModel(fields=[_val(k.replace("__", "."), v) for k, v in pairs.items()])


def test_all_six_checks_pass_and_fail() -> None:
    ruleset = {
        "category": "t",
        "rules": [
            {"id": "req", "field": "a.x", "check": "required", "enabled": True, "message": "m"},
            {"id": "rgx", "field": "a.fein", "check": "regex",
             "params": {"pattern": "^\\d{2}-\\d{7}$"}, "enabled": True, "message": "m"},
            {"id": "mn", "field": "a.rev", "check": "min",
             "params": {"value": 100}, "enabled": True, "message": "m"},
            {"id": "mx", "field": "a.emp", "check": "max",
             "params": {"value": 500}, "enabled": True, "message": "m"},
            {"id": "cmp", "field": "a.rev", "check": "compare",
             "params": {"field": "f.rev", "op": ">="}, "enabled": True, "message": "m"},
            {"id": "xd", "field": "a.name", "check": "crossDoc",
             "params": {"field": "l.name", "op": "=="}, "enabled": True, "message": "m"},
        ],
    }
    passing = _model(
        a__x="present", a__fein="57-1122334", a__rev="$2,450,000", a__emp="22",
        f__rev="$2,410,000", a__name="Riverside LLC", l__name="riverside llc",
    )
    results = {r.rule_id: r.passed for r in evaluate_ruleset(ruleset, passing)}
    assert results == {"req": True, "rgx": True, "mn": True, "mx": True, "cmp": True, "xd": True}

    failing = _model(
        a__x="", a__fein="BAD", a__rev="50", a__emp="9000",
        f__rev="$2,410,000", a__name="Alpha", l__name="Beta",
    )
    fres = {r.rule_id: r.passed for r in evaluate_ruleset(ruleset, failing)}
    assert fres == {"req": False, "rgx": False, "mn": False, "mx": False, "cmp": False, "xd": False}


def test_disabled_rules_are_skipped() -> None:
    ruleset = {"rules": [
        {"id": "on", "field": "a.x", "check": "required", "enabled": True},
        {"id": "off", "field": "a.y", "check": "required", "enabled": False},
    ]}
    ids = {r.rule_id for r in evaluate_ruleset(ruleset, _model(a__x="v"))}
    assert ids == {"on"}


async def test_publish_and_rollback(mem_session, mga_ctx) -> None:
    key = "verset"
    rs = RuleSet(tenant_id=mga_ctx.tenant_id, vertical=mga_ctx.vertical, key=key)
    mem_session.add(rs)
    await mem_session.flush()

    # v1 requires a.x ; v2 requires a.y
    v1 = {"rules": [{"id": "need_x", "field": "a.x", "check": "required", "enabled": True}]}
    v2 = {"rules": [{"id": "need_y", "field": "a.y", "check": "required", "enabled": True}]}
    for ver, rules in ((1, v1), (2, v2)):
        mem_session.add(
            RuleVersion(rule_set_id=rs.id, version=ver, status=RuleStatus.DRAFT, rules=rules)
        )
    await mem_session.commit()

    engine = DefaultRulesEngine()
    model = _model(a__x="present")  # has x, not y

    await engine.publish(mem_session, mga_ctx, key, 2)
    r2 = await engine.evaluate(mem_session, mga_ctx, key, model)
    assert {r.rule_id for r in r2} == {"need_y"} and not r2[0].passed  # v2 active, y missing

    await engine.rollback(mem_session, mga_ctx, key, 1)
    r1 = await engine.evaluate(mem_session, mga_ctx, key, model)
    assert {r.rule_id for r in r1} == {"need_x"} and r1[0].passed  # v1 active again, x present


def test_smoke_ruleset_is_valid_json() -> None:
    rs = load_ruleset()
    assert rs["category"] == "submission_validation"
    assert len(rs["rules"]) == 8
