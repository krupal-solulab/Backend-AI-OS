"""E&S Decision Core — Matching/Ranking Engine (Phase 2B, E&S-owned).

Not shared: this is the vertical-specific decision logic CORE_MODULES.md
reserves for each vertical (MGA gets an Appetite Engine; E&S gets this).
"""

from verticals.es.decision_core.carrier_profiles import CarrierProfile, load_carrier_panel
from verticals.es.decision_core.matching import CarrierMatch, HardExclusion, decide_market_match
from verticals.es.decision_core.seed_rules import (
    SUBMISSION_VALIDATION_KEY,
    seed_and_publish_carrier_rulesets,
    seed_and_publish_submission_validation_ruleset,
)

__all__ = [
    "SUBMISSION_VALIDATION_KEY",
    "CarrierMatch",
    "CarrierProfile",
    "HardExclusion",
    "decide_market_match",
    "load_carrier_panel",
    "seed_and_publish_carrier_rulesets",
    "seed_and_publish_submission_validation_ruleset",
]
