"""E&S vertical router. Empty in Phase 0.

The E&S developer mounts each workflow here with a single ``include_router(...)`` line
(e.g. market-matching) under ``/api/es/*`` — and edits ONLY this file to register.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/es", tags=["es"])

# Phase 2+: router.include_router(market_matching_router)
