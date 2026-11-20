"""
routes/git.py - Git status, Gemini-drafted commit messages, commit, and push.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from models import GitCommitRequest, GitDraftMessageRequest
from services import git_tools, key_store

router = APIRouter(prefix="/api/git", tags=["git"])


@router.get("/status")
def git_status(request: Request, project_id: str = Query(...)) -> Dict[str, Any]:
    project = request.app.state.db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        return git_tools.get_git_status(project.root_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/draft-message")
def draft_commit_message(payload: GitDraftMessageRequest, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    gemini = request.app.state.gemini

    if getattr(gemini, "_client_factory", None) is None:
        try:
            key_val, _ = key_store.load_api_key(gemini.key_path)
            has_key = bool(key_val)
        except RuntimeError:
            has_key = False
        if not has_key:
            raise HTTPException(
                status_code=400,
                detail="Configure a Gemini API key to draft commit messages.",
            )

    project = db.get_project(payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    status = git_tools.get_git_status(project.root_path)
    if not status.get("is_repo"):
        raise HTTPException(status_code=400, detail=status.get("error", "Not a git repo."))
    if not status.get("changed_files"):
        raise HTTPException(status_code=400, detail="Working tree is clean; nothing to commit.")

    diff_text = status.get("diff") or "\n".join(
        f"{f['status']} {f['path']}" for f in status["changed_files"]
    )
    try:
        draft = gemini.draft_commit_message(diff_text, model=payload.model)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    db.record_api_usage(payload.model, draft["usage"])
    return {
        "message": draft["message"],
        "diff_tokens": status.get("diff_tokens", 0),
        "usage": draft["usage"].model_dump(),
        "telemetry": db.get_rate_telemetry(model=payload.model),
    }


@router.post("/commit")
def git_commit(payload: GitCommitRequest, request: Request) -> Dict[str, Any]:
    project = request.app.state.db.get_project(payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        return git_tools.commit_changes(
            project.root_path,
            message=payload.message,
            stage_all=payload.stage_all,
            push=payload.push,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
