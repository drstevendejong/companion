"""
routes/workspace.py - File browser, manual attach preview, command runner, and patch undo.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request

from models import PatchRecord, RunCommandRequest
from services import bridge as bridge_svc
from services import checkpoints as cp_svc
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
    db = request.app.state.db
    project = db.get_project(payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        res = await runner.stream_project_command(
            getattr(request, "app", None),
            project.root_path,
            payload.command,
            configured_python=project.python_path,
            source="console",
            session_id=payload.session_id,
        )
        res["generated_md_files"] = bridge_svc.find_recent_markdown_outputs(
            project.root_path,
            res.get("output", ""),
        )
        if payload.is_test_run and payload.session_id and not res.get("cancelled"):
            raw_out = res.get("output") or ""
            has_traceback = (
                "Traceback (most recent call last):" in raw_out
                or "\nSyntaxError:" in raw_out
                or "\nAssertionError:" in raw_out
                or "\nImportError:" in raw_out
                or "\nModuleNotFoundError:" in raw_out
            )
            passed = (res.get("exit_code") == 0) and (not has_traceback)
            committed = None
            if passed and hasattr(db, "get_active_checkpoint"):
                committed = cp_svc.auto_commit_active_checkpoint_on_tests_pass(
                    db, project, payload.session_id, payload.command
                )
            res["checkpoint_committed"] = bool(committed)
            if hasattr(db, "list_checkpoints"):
                res["checkpoints"] = db.list_checkpoints(payload.session_id)
        return res
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/cancel-run")
def cancel_workspace_command(
    session_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
) -> Dict[str, Any]:
    count = runner.cancel_active_runs(session_id=session_id, run_id=run_id)
    return {"ok": True, "cancelled_count": count}


@router.get("/backups", response_model=List[PatchRecord])
def list_project_backups(
    request: Request,
    project_id: str = Query(...),
) -> List[PatchRecord]:
    from pathlib import Path as _Path

    db = request.app.state.db
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    patch_tools.prune_backup_directory(project.root_path, project.backup_dir)
    if hasattr(db, "list_active_checkpoint_dirs"):
        cp_svc.prune_checkpoints_directory(
            project.root_path,
            project.backup_dir,
            active_snapshot_dirs=set(db.list_active_checkpoint_dirs(project.id)),
        )
    records = db.list_patch_records(project_id)
    for rec in records:
        if rec.existed_before:
            rec.backup_exists = bool(rec.backup_path and _Path(rec.backup_path).is_file())
        else:
            rec.backup_exists = True
    return records


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


@router.post("/checkpoints/{checkpoint_id}/restore")
def restore_project_checkpoint(checkpoint_id: str, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    cp = db.get_checkpoint(checkpoint_id)
    if cp is None:
        raise HTTPException(status_code=404, detail="Checkpoint not found.")
    if cp.status == "restored":
        raise HTTPException(status_code=400, detail="Checkpoint has already been restored.")
    project = db.get_project(cp.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        res = cp_svc.restore_checkpoint_snapshot(
            project.root_path,
            cp.snapshot_dir,
            extra_created_files=cp.created_files,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    note = (
        f"Restored {res['restored_count']} modified file(s)"
        + (f" & removed {res['removed_count']} created file(s)." if res["removed_count"] else ".")
    )
    updated = db.resolve_checkpoint(cp.id, "restored", note)
    cancelled = db.cancel_pending_gates(cp.session_id, reason="Project state restored from savepoint")
    cp_svc.prune_checkpoints_directory(
        project.root_path,
        project.backup_dir,
        active_snapshot_dirs=set(db.list_active_checkpoint_dirs(project.id)),
    )
    return {
        "checkpoint": updated,
        "checkpoints": db.list_checkpoints(cp.session_id),
        "pending_gates": db.list_pending_gates(cp.session_id),
        "cancelled_gates": cancelled,
        **res,
    }


@router.post("/checkpoints/{checkpoint_id}/commit")
def commit_project_checkpoint(checkpoint_id: str, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    cp = db.get_checkpoint(checkpoint_id)
    if cp is None:
        raise HTTPException(status_code=404, detail="Checkpoint not found.")
    if cp.status != "active":
        raise HTTPException(status_code=400, detail=f"Checkpoint is already {cp.status}.")
    project = db.get_project(cp.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    updated = db.resolve_checkpoint(
        cp.id,
        "committed",
        "Committed manually by operator.",
    )
    cp_svc.prune_checkpoints_directory(
        project.root_path,
        project.backup_dir,
        active_snapshot_dirs=set(db.list_active_checkpoint_dirs(project.id)),
    )
    return {
        "checkpoint": updated,
        "checkpoints": db.list_checkpoints(cp.session_id),
    }
