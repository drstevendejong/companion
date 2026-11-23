"""
routes/sessions.py - Session management, model catalog, and token statistics.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from config import THINKING_LEVELS
from models import SessionCreate, SessionInfo, SessionUpdate
from services import local_scout

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("/models")
def get_model_catalog(
    request: Request,
    refresh: bool = Query(default=False),
    probe: bool = Query(default=False),
) -> Dict[str, Any]:
    gemini = getattr(request.app.state, "gemini", None)
    if gemini is not None and hasattr(gemini, "get_model_catalog"):
        return gemini.get_model_catalog(force_refresh=refresh, probe=probe)
    from services.model_catalog import get_fallback_catalog

    return get_fallback_catalog()


@router.get("", response_model=List[SessionInfo])
def list_sessions(
    request: Request,
    project_id: Optional[str] = Query(default=None),
) -> List[SessionInfo]:
    return request.app.state.db.list_sessions(project_id=project_id)


@router.post("", response_model=SessionInfo)
def create_session(payload: SessionCreate, request: Request) -> SessionInfo:
    db = request.app.state.db
    if db.get_project(payload.project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return db.create_session(payload)


@router.get("/{session_id}")
def get_session_detail(session_id: str, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    gemini = getattr(request.app.state, "gemini", None)
    session = db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    messages = db.list_messages(session_id)
    if session.token_stats.last_context_tokens == 0 and session.token_stats.total_tokens > 0:
        for msg in reversed(messages):
            if msg.role == "model" and msg.token_usage:
                u = msg.token_usage
                session.token_stats.last_context_tokens = u.total_tokens or (
                    u.input_tokens + u.output_tokens + u.thinking_tokens
                )
                break
    return {
        "session": session,
        "messages": messages,
        "pending_gates": db.list_pending_gates(session_id),
        "checkpoints": db.list_checkpoints(session_id) if hasattr(db, "list_checkpoints") else [],
        "telemetry": local_scout.get_telemetry_snapshot(db, gemini, model=session.model),
    }


@router.patch("/{session_id}", response_model=SessionInfo)
def update_session(session_id: str, payload: SessionUpdate, request: Request) -> SessionInfo:
    updated = request.app.state.db.update_session(session_id, payload)
    if updated is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return updated


@router.delete("/{session_id}")
def delete_session(session_id: str, request: Request) -> Dict[str, bool]:
    deleted = request.app.state.db.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"deleted": True}
