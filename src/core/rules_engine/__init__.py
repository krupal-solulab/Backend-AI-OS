"""Rules Engine (Phase 1) — generic, versioned evaluator.

Runs the 6 check types (``required · regex · min · max · compare · crossDoc``) over the
FE Rules Console rule-set JSON shape::

    {"category": "...",
     "rules": [{"id","label","field","check","params?","severity","message","enabled"}]}

against a flattened view of an ``ExtractedModel`` (field name → value, plus
``documents.<kind>.present`` flags). "Required document present" is therefore expressed
as ordinary ``required`` rules — never hardcoded here. Rule sets are versioned via
``RuleSet`` → ``RuleVersion`` with publish/rollback.
"""

from core.rules_engine.engine import (
    DefaultRulesEngine,
    RulesEngine,
    evaluate_ruleset,
)

__all__ = ["DefaultRulesEngine", "RulesEngine", "evaluate_ruleset"]
