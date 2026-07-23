"""MGA · Submission Triage (Workflow_1) — the first real workflow."""

from verticals.mga.submission_triage.router import router
from verticals.mga.submission_triage.service import TriageService

__all__ = ["TriageService", "router"]
