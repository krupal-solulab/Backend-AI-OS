"""Broker Communication Copilot thresholds — DATA, not code (mirrors
``renewal_management/config.py``). Retune these, not the drafting engine, to change tone
calibration or compliance gating.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrokerCopilotConfig:
    # volume tiers that get the warmer "strategic"/"high" treatment vs a neutral default
    strategic_tiers: tuple[str, ...] = ("strategic", "high")
    # below this many business days without a broker reply → NO_RESPONSE_FOLLOWUP is due
    follow_up_after_business_days: int = 7
    # comm types that always require a compliance-review gate before send (non-renewal
    # notices carry state notice-period / content rules — never sent unreviewed)
    compliance_required_types: tuple[str, ...] = ("NON_RENEWAL_NOTICE",)
    # comm types treated as "sensitive" — juniors route these to a senior instead of
    # sending directly (parity with the Triage/Renewal authority-cap pattern)
    sensitive_types: tuple[str, ...] = ("NON_RENEWAL_NOTICE", "CONSISTENCY_FLAG_FOLLOWUP")
