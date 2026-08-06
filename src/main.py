"""FastAPI application entry point (Phase 0 skeleton).

Mounts the shared core health route + both (empty) vertical routers. Run with:
    uvicorn main:app --app-dir src --reload
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from verticals.es.router import router as es_router
from verticals.mga.router import router as mga_router

app = FastAPI(title="Insurance OS Backend", version="0.0.0")

# Dev CORS — allow the local FE dev server (any localhost origin) to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

core_router = APIRouter(prefix="/api/core", tags=["core"])


@core_router.get("/health")
async def health() -> dict[str, int]:
    """Liveness probe. Phase marker confirms which milestone this build is at."""
    return {"phase": 0}


app.include_router(core_router)
app.include_router(mga_router)
app.include_router(es_router)
