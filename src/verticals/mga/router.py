"""MGA vertical router. Empty in Phase 0.

The MGA developer mounts each workflow here with a single ``include_router(...)`` line
(e.g. submission-triage) under ``/api/mga/*`` — and edits ONLY this file to register.
"""

from fastapi import APIRouter

from verticals.mga.renewal_management.router import router as renewal_management_router
from verticals.mga.submission_triage import router as submission_triage_router

router = APIRouter(prefix="/api/mga", tags=["mga"])

router.include_router(submission_triage_router)
router.include_router(renewal_management_router)
