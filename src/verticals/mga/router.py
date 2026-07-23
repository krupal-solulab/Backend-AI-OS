"""MGA vertical router. Empty in Phase 0.

The MGA developer mounts each workflow here with a single ``include_router(...)`` line
(e.g. submission-triage) under ``/api/mga/*`` — and edits ONLY this file to register.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/mga", tags=["mga"])

# Phase 2+: router.include_router(submission_triage_router)
