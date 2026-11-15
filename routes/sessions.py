"""
routes/sessions.py - Session management, model catalog, and token statistics.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from config import AVAILABLE_MODELS, THINKING_LEVELS
from models import SessionCreate, SessionInfo, SessionUpdate

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("/models")
def get_model_catalog() -> Dict[str, Any]:
    return {
        "models": [
            {"id": m_id, "label": label, "context_limit": ctx}
            for m_id, label, ctx in AVAILABLE_MODELS
        ],
        "thinking_levels": THINKING_LEVELS,
    }


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
    session = db.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {
        "session": session,
        "messages": db.list_messages(session_id),
        "pending_gates": db.list_pending_gates(session_id),
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
