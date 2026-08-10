"""Bind Order & Issuance thresholds — DATA, not code (mirrors ``EndorsementConfig`` /
``AppetiteConfig``). Every value is a placeholder from the PRD/dataset, per the project's
own convention — validate with a real design partner before treating as production-ready.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BindIssuanceConfig:
    # MBI-03: a worksheet older than this many days requires a return to Quoting &
    # Rating Support for recalculation before binding may proceed.
    worksheet_staleness_days: int = 14
    # MBI-01: a requested bind premium must match the finalized worksheet to within this
    # tolerance — anything beyond is a hand-edit / fidelity violation, never silently
    # accepted or rounded away.
    premium_fidelity_tolerance: float = 0.01
    # MBI-05: an issuance field difference beyond this tolerance (for numeric fields) is
    # a material discrepancy, never assumed a rounding artifact.
    issuance_numeric_tolerance: float = 0.01
    # MBI-07: default reminder lead times (days before the obligation's due date).
    obligation_reminder_days: tuple[int, ...] = (15, 5)
