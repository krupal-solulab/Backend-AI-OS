"""MGA Decision Core — the Appetite Engine.

The ONLY decision logic in the system. It orchestrates the shared validation rule
results + the extracted model and maps them to the frozen ``Decision`` DTO
(PROCEED / REQUEST_INFO / DECLINE) plus a transparent 0–100 appetite score.

Compound appetite rules that the generic 6-check engine cannot express (excluded-class
lists, compound severity ceilings, cross-doc variance/disclosure, timing, loss-trend,
extraction-confidence) live HERE as MGA-specific logic with ALL thresholds kept as data
(``AppetiteConfig``) so they stay tunable without code changes. Nothing here is in
``core/`` — appetite is MGA-specific by design.
"""

from verticals.mga.decision_core.appetite import AppetiteEngine
from verticals.mga.decision_core.config import AppetiteConfig

__all__ = ["AppetiteConfig", "AppetiteEngine"]
