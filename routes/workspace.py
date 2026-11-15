"""
routes/workspace.py - File browser, manual attach preview, command runner, and patch undo.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request

from models import PatchRecord, RunCommandRequest
from services import fs_tools, patch_tools, runner

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


@router.get("/tree")
def get_project_tree(
    request: Request,
    project_id: str = Query(...),
    subpath: str = Query(default="."),
    max_depth: int = Query(default=0, ge=0, le=8),
) -> Dict[str, Any]:
    project = request.app.state.db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        return fs_tools.list_tree(
            project.root_path,
            subpath=subpath,
            max_depth=max_depth,
            extra_ignores=project.ignore_patterns,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/file-preview")
def preview_file_for_attach(
    request: Request,
    project_id: str = Query(...),
    path: str = Query(...),
    start_line: int | None = Query(default=None, ge=1),
    end_line: int | None = Query(default=None, ge=1),
) -> Dict[str, Any]:
    project = request.app.state.db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        if start_line is not None and end_line is not None:
            return fs_tools.read_lines(project.root_path, path, start_line, end_line)
        return fs_tools.read_file(project.root_path, path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/run")
async def run_workspace_command(payload: RunCommandRequest, request: Request) -> Dict[str, Any]:
    project = request.app.state.db.get_project(payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        return await runner.run_project_command(
            project.root_path,
            payload.command,
            configured_python=project.python_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/backups", response_model=List[PatchRecord])
def list_project_backups(
    request: Request,
    project_id: str = Query(...),
) -> List[PatchRecord]:
    db = request.app.state.db
    if db.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return db.list_patch_records(project_id)


@router.post("/undo/{patch_id}")
def undo_project_patch(patch_id: str, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    rec = db.get_patch_record(patch_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Patch record not found.")
    if rec.reverted:
        raise HTTPException(status_code=400, detail="Patch has already been reverted.")
    project = db.get_project(rec.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        result = patch_tools.undo_patch_on_disk(
            project.root_path,
            rec.file_path,
            rec.backup_path,
            rec.existed_before,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.mark_patch_reverted(patch_id)
    return result
