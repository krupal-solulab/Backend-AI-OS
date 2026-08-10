"""Schedule adjustment suggestion config — DATA, not code (mirrors ``AppetiteConfig``).
QR-03/FR-4: a credit/debit suggestion must always have a stated, grounded basis in
extracted submission data — these are the keyword signals the sample dataset illustrates
(loss trend, safety documentation). Per the PRD's own risk register, this is illustrative,
not validated against real underwriting practice — tune per class during discovery.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuotingConfig:
    # loss_history_trend phrases that support a suggested credit (favorable trend)
    improving_trend_keywords: tuple[str, ...] = ("improving",)
    # loss_history_trend phrases that support a suggested debit (unfavorable trend)
    worsening_trend_keywords: tuple[str, ...] = ("worsening",)
    # suggested adjustment magnitude when a grounded basis is found
    improving_credit_pct: float = -8.0
    worsening_debit_pct: float = 15.0
    # additional credit stacked on top of a favorable trend when documented
    safety_program_extra_credit_pct: float = 0.0
