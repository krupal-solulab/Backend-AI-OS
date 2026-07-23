"""FastAPI application entry point (Phase 0 skeleton).

Mounts the shared core health route + both (empty) vertical routers. Run with:
    uvicorn main:app --app-dir src --reload
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from verticals.es.router import router as es_router
from verticals.mga.router import router as mga_router

app = FastAPI(title="Insurance OS Backend", version="0.0.0")

core_router = APIRouter(prefix="/api/core", tags=["core"])


@core_router.get("/health")
async def health() -> dict[str, int]:
    """Liveness probe. Phase marker confirms which milestone this build is at."""
    return {"phase": 0}


app.include_router(core_router)
app.include_router(mga_router)
app.include_router(es_router)
