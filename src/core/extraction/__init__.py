"""Extraction Core (Phase 1) — classify each document + extract a CITED, typed field
model from the semi-structured ``Key: Value`` fixture text (no OCR).

Tolerant of the VARIABLE document set (2–5+ docs): a missing document (e.g. no
financials) or an extra one (SOV) never raises — extraction returns whatever the
present documents yield. "Which docs are required" is a Phase-1 RULE, not extraction.
"""

from core.extraction.service import DefaultExtractionService, ExtractionService

__all__ = ["DefaultExtractionService", "ExtractionService"]
