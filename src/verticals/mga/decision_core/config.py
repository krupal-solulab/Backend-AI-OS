"""Appetite thresholds — DATA, not code. Every value here is a placeholder from the
Workflow_1 strawman spec (Validation_Rules_Test_Dataset.md) and is meant to be replaced
with the design partner's real underwriting rules without touching the engine."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AppetiteConfig:
    # HR-01 excluded class codes (leading class-code token)
    excluded_class_codes: frozenset[str] = frozenset({"96065"})
    # HR-02 compound loss-severity ceiling
    severity_ceiling_incurred: float = 250_000
    severity_revenue_threshold: float = 5_000_000
    fall_height_ft: int = 15
    fall_prone_class_keywords: tuple[str, ...] = ("roof", "roofing", "construction")
    # HR-03 licensed / in-appetite states (full names + USPS abbreviations)
    licensed_states: frozenset[str] = frozenset({
        "south carolina", "north carolina", "georgia", "tennessee", "alabama",
        "mississippi", "virginia", "sc", "nc", "ga", "tn", "al", "ms", "va",
    })
    # HR-04 per-class binding-authority revenue ceiling
    revenue_authority_ceiling: float = 50_000_000
    # CR-01 minimum loss-run history (years)
    min_loss_years: int = 5
    # CC-01 revenue consistency variance
    revenue_variance_pct: float = 0.15
    # CC-02 loss-disclosure materiality + phrases that assert "no losses"
    disclosure_materiality: float = 10_000
    disclosure_phrases: tuple[str, ...] = (
        "no significant losses", "no losses", "no significant", "no claims",
    )
    # TR-01 minimum submission-to-effective lead time (business days)
    min_lead_time_business_days: int = 5
    # EC-01 extraction-confidence floor + how many low-confidence fields trip manual review
    confidence_floor: float = 0.8
    low_confidence_min_fields: int = 2
    # scoring
    soft_flag_penalty: int = 15
    request_info_score_cap: int = 60
    # informational
    loss_trend_worsening_keywords: tuple[str, ...] = field(
        default=("worsening", "increasing"))
