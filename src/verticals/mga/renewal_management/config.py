"""Renewal comparison thresholds — DATA, not code (PRD §7, RN-01..RN-12).

Every value is a placeholder from the Workflow_2 strawman spec; change these (not the
engine) to retune. New rules are authored in the published rule sets, not here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RenewalConfig:
    # RN-01 revenue growth / RN-02 revenue decline (fractional change vs prior term).
    # PRD placeholder was 25% each; decline set to 15% so the dataset's 20.6% decline
    # case (renewal_06, $680k→$540k rental income) is caught. Pure config — retune freely.
    revenue_growth_pct: float = 0.25
    revenue_decline_pct: float = 0.15
    # RN-03 headcount change (either direction)
    headcount_change_pct: float = 0.30
    # RN-06 loss deterioration: >= this many new claims in the expiring term, OR any single
    # new claim incurred above the severity threshold.
    max_new_claims_in_term: int = 2
    new_claim_severity: float = 50_000
    # RN-07 favorable resolution: claim closed at paid this fraction (or more) below prior reserve
    favorable_close_pct: float = 0.20
    # RN-11 lapse risk: fewer than this many business days from received → expiration
    min_lead_time_business_days: int = 5
    # EC confidence floor (degraded-scan → manual review), reused parity with Triage
    confidence_floor: float = 0.8
    low_confidence_min_fields: int = 2
    # scoring
    change_flag_penalty: int = 15
    # answers on the questionnaire that mean "no change" (case-insensitive substring match)
    negative_answers: tuple[str, ...] = ("no", "none", "n/a", "no change", "flat")
