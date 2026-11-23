"""
routes/git.py - Git status, Gemini-drafted commit messages, commit, and push.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request

from models import GitCommitRequest, GitDraftMessageRequest, GitUpdateHeadersRequest
from routes.chat import _get_effective_scout_settings
from services.key_store import has_configured_api_key
from services import git_tools, local_scout
from services.gemini_client import prepare_tool_gate_preview

router = APIRouter(prefix="/api/git", tags=["git"])

_HEADER_ELIGIBLE_EXTS = {
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".html",
    ".htm",
    ".go",
    ".rs",
    ".java",
}


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

    if not has_configured_api_key(gemini):
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
    scout_cfg = _get_effective_scout_settings(db, gemini)
    use_scout = bool(payload.use_scout or scout_cfg.get("enabled"))
    try:
        if use_scout:
            draft = local_scout.draft_git_commit_with_scout(gemini, db, diff_text)
            model_used = draft.get("model_used") or payload.model
        else:
            draft = gemini.draft_commit_message(diff_text, model=payload.model)
            model_used = payload.model
            db.record_api_usage(model_used, draft["usage"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "message": draft["message"],
        "model_used": model_used,
        "used_scout": use_scout,
        "diff_tokens": status.get("diff_tokens", 0),
        "usage": draft["usage"].model_dump(),
        "telemetry": local_scout.get_telemetry_snapshot(db, gemini, model=model_used),
    }


@router.post("/update-headers")
def update_changed_file_headers(
    payload: GitUpdateHeadersRequest, request: Request
) -> Dict[str, Any]:
    db = request.app.state.db
    gemini = request.app.state.gemini

    scout_cfg = _get_effective_scout_settings(db, gemini)
    if not scout_cfg.get("enabled"):
        raise HTTPException(
            status_code=400,
            detail="Enable Local AI Scout (with a configured API key) to draft header updates.",
        )

    project = db.get_project(payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    target_paths: List[str] = [p.strip() for p in (payload.paths or []) if p and p.strip()]
    if not target_paths:
        status = git_tools.get_git_status(project.root_path)
        if not status.get("is_repo"):
            raise HTTPException(status_code=400, detail=status.get("error", "Not a git repo."))
        for entry in status.get("changed_files") or []:
            st = str(entry.get("status") or "")
            p = str(entry.get("path") or "").strip()
            if not p or "D" in st:
                continue
            ext = "." + p.rsplit(".", 1)[-1].lower() if "." in p.rsplit("/", 1)[-1] else ""
            if ext in _HEADER_ELIGIBLE_EXTS:
                target_paths.append(p)

    if not target_paths:
        raise HTTPException(
            status_code=400,
            detail="No modified header-eligible source files found to update.",
        )

    try:
        hdr_res = local_scout.draft_header_updates(
            gemini,
            db,
            project,
            paths=target_paths,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    session_id = payload.session_id
    staged_paths: List[str] = []
    if session_id and db.get_session(session_id) is not None:
        for patch in hdr_res.get("proposed_patches") or []:
            p_args = {
                "path": patch["path"],
                "content": patch["content"],
                "summary": f"Update header comment ({patch['path']}) via Local AI Scout",
            }
            p_preview = prepare_tool_gate_preview(project, "write_file", p_args)
            ui_preview = {
                k: v for k, v in p_preview.items()
                if k not in ("prepared_result", "prepared_patch")
            }
            db.create_gate(
                session_id=session_id,
                tool_name="write_file",
                args=p_args,
                preview=ui_preview,
            )
            staged_paths.append(patch["path"])
        pending_gates = db.list_pending_gates(session_id)
    else:
        staged_paths = [p["path"] for p in (hdr_res.get("proposed_patches") or [])]
        pending_gates = []

    unchanged = [p for p in target_paths if p not in staged_paths]
    return {
        "staged_count": len(staged_paths),
        "staged_paths": staged_paths,
        "unchanged_paths": unchanged,
        "pending_gates": pending_gates,
        "models_used": hdr_res.get("models_used", []),
        "tokens_used": hdr_res.get("tokens_used", 0),
        "telemetry": local_scout.get_telemetry_snapshot(db, gemini),
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
