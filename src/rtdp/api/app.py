"""FastAPI application factory for the Stage 2A serving layer.

``create_app`` accepts an optional :class:`Settings` so tests can point the app at a temp
``file://`` warehouse. The catalog is built once at startup (lifespan) and cached on
``app.state``; routes load the table per request. The app is strictly read-only — there is
no write/ingestion path here (ingestion stays the Stage 1 CLI).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import query
from ..catalog import build_catalog
from ..config import Settings
from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the catalog once at startup; tolerate failure so /health can still report 503."""
    settings: Settings = app.state.settings
    try:
        app.state.catalog = build_catalog(settings)
    except Exception:  # noqa: BLE001 — a broken catalog must not stop the app from booting
        app.state.catalog = None
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="rtdp serving layer",
        version="0.1.0",
        summary="Read-only HTTP API over the Stage 1 Iceberg lakehouse (Stage 2A).",
        description=(
            "Typed, queryable reads over the bronze OpenSky state-vector table, including "
            "Iceberg snapshot/time-travel reads. Read-only and local-first: no auth, rate "
            "limiting, caching, or write path yet; Stage D agent integration is deferred."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings or Settings()
    app.state.catalog = None

    @app.exception_handler(query.AsOfConflictError)
    async def _as_of_conflict(_request: Request, exc: query.AsOfConflictError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(query.SnapshotNotFoundError)
    async def _snapshot_not_found(
        _request: Request, exc: query.SnapshotNotFoundError
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    app.include_router(router)
    return app
