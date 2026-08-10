"""Portfolio & Book Performance Reporting thresholds — DATA, not code (mirrors every
other MGA workflow's config.py).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PortfolioConfig:
    # PBR-03: a class/carrier loss-ratio segment is annotated as low-volume below this
    # bound-account count (same threshold discipline as Appetite Governance's AG-06).
    low_volume_threshold: int = 10
    # PBR-03: a segment is "single-event driven" when one claim accounts for at least
    # this share of total incurred losses in the segment.
    single_event_share_threshold: float = 0.60
    # PBR-06: a broker's period-over-period production change is a "significant decline"
    # worth noting factually (not speculatively) at or beyond this magnitude.
    significant_decline_pct: float = -25.0
