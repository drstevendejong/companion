"""
app.py - FastAPI application factory, router registration, and static UI serving.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Set

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import APP_ROOT, DB_PATH
from routes import bridge, cdp, chat, git, lifecycle, projects, sessions, settings, workspace
from services.gemini_client import GeminiGateway
from services.lifecycle import LifecycleTracker
from storage.db import CompanionDB


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.lifecycle_sockets = set()
    try:
        yield
    finally:
        sockets: Set[WebSocket] = set(getattr(app.state, "lifecycle_sockets", set()))
        for ws in sockets:
            try:
                await ws.send_text('{"type":"server_shutdown"}')
            except Exception:
                pass
            try:
                await ws.close(code=4001, reason="server_shutdown")
            except Exception:
                pass


def create_app(
    db_path: Optional[Path] = None,
    gemini_gateway: Optional[GeminiGateway] = None,
    auto_shutdown: bool = False,
) -> FastAPI:
    app = FastAPI(title="Gemini Vibe Companion", version="0.1.0", lifespan=_lifespan)
    app.state.db = CompanionDB(db_path or DB_PATH)
    app.state.gemini = gemini_gateway or GeminiGateway()
    app.state.lifecycle = LifecycleTracker(enabled=auto_shutdown)
    app.state.lifecycle_sockets = set()

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    app.include_router(projects.router)
    app.include_router(sessions.router)
    app.include_router(chat.router)
    app.include_router(bridge.router)
    app.include_router(workspace.router)
    app.include_router(git.router)
    app.include_router(cdp.router)
    app.include_router(settings.router)
    app.include_router(lifecycle.router)

    static_dir = APP_ROOT / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/", include_in_schema=False)
        def serve_index() -> FileResponse:
            return FileResponse(str(static_dir / "index.html"))

    return app
