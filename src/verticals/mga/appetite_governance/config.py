"""Appetite Governance thresholds — DATA, not code (mirrors every other MGA workflow's
config.py). AG-04's suppression discipline in particular is illustrative, per the PRD's
own risk register — validate with real underwriting leadership during discovery.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GovernanceConfig:
    # AG-04: an override pattern is suppressed unless it has at least this many
    # instances AND at least this many are undocumented. "Undocumented" is deliberately
    # narrow (exactly no reason logged) so the count stays strictly grounded to what was
    # actually recorded — a vague-but-present reason (e.g. "broker requested exception,
    # no further detail") is a separate, softer signal, not folded into this count.
    override_pattern_min_count: int = 2
    override_pattern_min_undocumented: int = 2
    undocumented_phrases: tuple[str, ...] = ("no reason logged",)
    # AG-06: a class/carrier segment is annotated as low-volume below this account count.
    low_volume_threshold: int = 10
    # AG-06: an account is "near the ceiling" at or above this fraction of the limit.
    near_ceiling_pct: float = 0.90
