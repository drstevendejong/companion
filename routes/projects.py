"""
routes/projects.py - Project profile CRUD, local filesystem picker, and Directives .md verification.
"""

from __future__ import annotations

import os
import string
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from models import ProjectCreate, ProjectProfile, ProjectUpdate
from services import fs_tools
from services.token_utils import estimate_tokens

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _list_filesystem_roots() -> List[str]:
    if os.name == "nt":
        roots: List[str] = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if Path(drive).exists():
                roots.append(drive)
        return roots
    return ["/"]


@router.get("/browse-fs")
def browse_local_filesystem(
    path: Optional[str] = Query(default=None),
    include_files: bool = Query(default=True),
) -> Dict[str, Any]:
    """Lists directories and files on the local machine for project config pickers."""
    target = Path(path).expanduser() if path and path.strip() else Path.home()
    if not target.exists() or not target.is_dir():
        target = Path.home()
    resolved = target.resolve()

    parent_path = str(resolved.parent) if resolved.parent != resolved else None
    entries: List[Dict[str, Any]] = []
    try:
        children = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for child in children:
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if not is_dir and not include_files:
                continue
            entries.append({
                "name": child.name,
                "path": str(child),
                "is_dir": is_dir,
            })
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Permission denied: {exc}") from exc

    return {
        "current_path": str(resolved),
        "parent_path": parent_path,
        "home_path": str(Path.home()),
        "roots": _list_filesystem_roots(),
        "entries": entries,
    }


@router.get("", response_model=List[ProjectProfile])
def list_projects(request: Request) -> List[ProjectProfile]:
    return request.app.state.db.list_projects()


@router.post("", response_model=ProjectProfile)
def create_project(payload: ProjectCreate, request: Request) -> ProjectProfile:
    root = Path(payload.root_path).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {root}")
    normalized = payload.model_copy(update={"root_path": str(root)})
    return request.app.state.db.create_project(normalized)


@router.patch("/{project_id}", response_model=ProjectProfile)
def update_project(project_id: str, payload: ProjectUpdate, request: Request) -> ProjectProfile:
    if payload.root_path is not None:
        root = Path(payload.root_path).expanduser().resolve()
        if not root.is_dir():
            raise HTTPException(status_code=400, detail=f"Directory does not exist: {root}")
        payload = payload.model_copy(update={"root_path": str(root)})
    updated = request.app.state.db.update_project(project_id, payload)
    if updated is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return updated


@router.delete("/{project_id}")
def delete_project(project_id: str, request: Request) -> Dict[str, bool]:
    deleted = request.app.state.db.delete_project(project_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"deleted": True}


@router.get("/{project_id}/directives")
def get_directives_info(project_id: str, request: Request) -> Dict[str, Any]:
    project = request.app.state.db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    if not project.directives_path:
        return {"configured": False, "exists": False, "estimated_tokens": 0}
    try:
        dpath = fs_tools.resolve_safe_path(project.root_path, project.directives_path)
    except Exception as exc:
        return {"configured": True, "exists": False, "error": str(exc), "estimated_tokens": 0}
    if not dpath.is_file():
        return {"configured": True, "exists": False, "path": project.directives_path, "estimated_tokens": 0}
    text = dpath.read_text(encoding="utf-8", errors="replace")
    return {
        "configured": True,
        "exists": True,
        "path": project.directives_path,
        "estimated_tokens": estimate_tokens(text),
    }
