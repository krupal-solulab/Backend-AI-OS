"""E&S vertical router.

The E&S developer mounts each workflow here with a single ``include_router(...)`` line
(e.g. market-matching) under ``/api/es/*`` — and edits ONLY this file to register.
"""

from fastapi import APIRouter

from verticals.es.workflows.agent_communication.router import router as agent_communication_router
from verticals.es.workflows.binder_issuance.router import router as binder_issuance_router
from verticals.es.workflows.market_matching.router import router as market_matching_router
from verticals.es.workflows.package_assembly.router import router as package_assembly_router
from verticals.es.workflows.quote_comparison.router import router as quote_comparison_router

router = APIRouter(prefix="/api/es", tags=["es"])

router.include_router(market_matching_router)
router.include_router(package_assembly_router)
router.include_router(agent_communication_router)
router.include_router(quote_comparison_router)
router.include_router(binder_issuance_router)
