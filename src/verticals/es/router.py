"""E&S vertical router.

The E&S developer mounts each workflow here with a single ``include_router(...)`` line
(e.g. market-matching) under ``/api/es/*`` — and edits ONLY this file to register.
"""

from fastapi import APIRouter

from verticals.es.workflows.market_matching.router import router as market_matching_router

router = APIRouter(prefix="/api/es", tags=["es"])

router.include_router(market_matching_router)
