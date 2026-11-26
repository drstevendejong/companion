"""
services/checkpoints.py - Project-level transactional savepoints & full rollback.

Before any write batch begins, snapshots all non-ignored project files into
<project_root>/<backup_dir>/.checkpoints/<timestamp>_<id>/ so the user can
restore the entire project cleanly if a session crashes, hits rate limits, or
fails tests—and auto-commits / replaces the saved state when tests pass.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from config import (
    CHECKPOINT_MAX_AGE_DAYS,
    CHECKPOINT_MAX_BYTES,
    CHECKPOINT_MAX_FILE_BYTES,
    CHECKPOINT_MAX_KEEP,
    CHECKPOINT_MAX_SNAPSHOT_BYTES,
)
from services.fs_tools import _is_ignored, _merge_ignores, resolve_safe_path


def _resolve_checkpoints_root(project_root: str | Path, backup_dir_rel: str) -> Path:
    root = Path(project_root).resolve()
    backup_base = Path(backup_dir_rel or "backup")
    if not backup_base.is_absolute():
        backup_base = (root / backup_base).resolve()
    return backup_base / ".checkpoints"


def _dir_size_bytes(directory: Path) -> int:
    total = 0
    try:
        for p in directory.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _checkpoint_ignores(project_root: Path, backup_dir_rel: str, extra_ignores: Optional[Sequence[str]]) -> Set[str]:
    merged = set(_merge_ignores(extra_ignores))
    merged.add(".checkpoints")
    if backup_dir_rel:
        bpath = Path(backup_dir_rel)
        if not bpath.is_absolute():
            try:
                rel = (project_root / bpath).resolve().relative_to(project_root)
                if rel.parts:
                    merged.add(rel.parts[0])
            except Exception:
                pass
    return merged


def list_project_relative_files(
    project_root: str | Path,
    backup_dir_rel: str,
    extra_ignores: Optional[Sequence[str]] = None,
) -> Set[str]:
    """Lists all non-ignored project files relative to project_root."""
    root = Path(project_root).resolve()
    ignores = _checkpoint_ignores(root, backup_dir_rel, extra_ignores)
    out: Set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath)
        rel_dir = cur.relative_to(root).as_posix()
        dirnames[:] = sorted(
            d for d in dirnames
            if not _is_ignored(d, f"{rel_dir}/{d}".lstrip("./"), ignores, is_dir=True)
        )
        for fname in sorted(filenames):
            fpath = cur / fname
            rel_posix = fpath.relative_to(root).as_posix()
            if _is_ignored(fname, rel_posix, ignores, is_dir=False):
                continue
            out.add(rel_posix)
    return out


def create_checkpoint_snapshot(
    project_root: str | Path,
    backup_dir_rel: str,
    checkpoint_id: str,
    extra_ignores: Optional[Sequence[str]] = None,
    active_snapshot_dirs: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """
    Initializes a lean Copy-on-Write Savepoint directory and manifest.json.
    Individual pre-edit files are copied lazily into <snapshot_dir>/files/ only
    when first modified or deleted, avoiding multi-megabyte full-repo duplicates.
    """
    root = Path(project_root).resolve()
    cp_root = _resolve_checkpoints_root(root, backup_dir_rel)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    short_id = checkpoint_id.replace("-", "")[:8]
    snapshot_dir = cp_root / f"{stamp}_{short_id}"
    files_dir = snapshot_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "checkpoint_id": checkpoint_id,
        "mode": "cow",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "baseline_files": [],
        "modified_files": [],
        "created_files": [],
        "skipped_large_files": [],
    }
    (snapshot_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    prune_checkpoints_directory(
        root,
        backup_dir_rel,
        active_snapshot_dirs=set(active_snapshot_dirs or []) | {str(snapshot_dir)},
    )

    return {
        "snapshot_dir": str(snapshot_dir),
        "files_count": 0,
        "total_bytes": 0,
    }


def record_checkpoint_touch(
    snapshot_dir: str | Path,
    rel_path: str,
    existed_before: bool,
    source_backup_path: Optional[str | Path] = None,
) -> None:
    """
    Records a modified or newly created file in manifest.json.
    In Copy-on-Write mode, copies the pre-edit file from source_backup_path on its
    first modification/deletion so full-project restore stays intact at tiny disk cost.
    """
    snap = Path(snapshot_dir)
    manifest_path = snap / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return

    norm = rel_path.replace("\\", "/").lstrip("./")
    baseline_list: List[str] = manifest.setdefault("baseline_files", [])
    baseline_set = set(baseline_list)
    mods: List[str] = manifest.setdefault("modified_files", [])
    created: List[str] = manifest.setdefault("created_files", [])
    created_set = set(created)

    if norm in created_set:
        pass
    elif existed_before:
        if norm not in baseline_set:
            if source_backup_path:
                src_bkp = Path(source_backup_path)
                try:
                    if src_bkp.is_file() and src_bkp.stat().st_size <= CHECKPOINT_MAX_FILE_BYTES:
                        dest = snap / "files" / norm
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_bkp, dest)
                        baseline_list.append(norm)
                        baseline_set.add(norm)
                    else:
                        skipped = manifest.setdefault("skipped_large_files", [])
                        if norm not in skipped:
                            skipped.append(norm)
                except OSError:
                    pass
            else:
                baseline_list.append(norm)
                baseline_set.add(norm)
        if norm not in mods:
            mods.append(norm)
    else:
        if norm not in created_set:
            created.append(norm)

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def restore_checkpoint_snapshot(
    project_root: str | Path,
    snapshot_dir: str | Path,
    extra_created_files: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """
    Restores all files modified/deleted since checkpoint creation back to their exact
    saved snapshot content, and removes files created under this checkpoint.
    """
    root = Path(project_root).resolve()
    snap = Path(snapshot_dir)
    manifest_path = snap / "manifest.json"
    files_dir = snap / "files"

    if not manifest_path.is_file() or not files_dir.is_dir():
        raise FileNotFoundError(f"Checkpoint snapshot missing on disk: {snap}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    baseline_files: List[str] = manifest.get("baseline_files") or []
    baseline_set = set(baseline_files)
    created_files: List[str] = list(manifest.get("created_files") or [])
    for extra in extra_created_files or []:
        if extra not in created_files:
            created_files.append(extra)

    restored_files: List[str] = []
    removed_files: List[str] = []

    for rel in baseline_files:
        src = files_dir / rel
        if not src.is_file():
            continue
        dest = resolve_safe_path(root, rel)
        changed = not dest.is_file()
        if not changed:
            try:
                if dest.stat().st_size != src.stat().st_size or dest.read_bytes() != src.read_bytes():
                    changed = True
            except OSError:
                changed = True
        if changed:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            restored_files.append(rel)

    for rel in created_files:
        if rel in baseline_set:
            continue
        try:
            target = resolve_safe_path(root, rel)
        except Exception:
            continue
        if target.is_file():
            target.unlink()
            removed_files.append(rel)

    return {
        "restored_count": len(restored_files),
        "removed_count": len(removed_files),
        "restored_files": restored_files,
        "removed_files": removed_files,
    }


def prune_checkpoints_directory(
    project_root: str | Path,
    backup_dir_rel: str,
    *,
    active_snapshot_dirs: Optional[Set[str]] = None,
    max_keep: int = CHECKPOINT_MAX_KEEP,
    max_age_days: int = CHECKPOINT_MAX_AGE_DAYS,
    max_bytes: int = CHECKPOINT_MAX_BYTES,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Self-cleans old committed/restored checkpoints while always keeping active ones."""
    cp_root = _resolve_checkpoints_root(project_root, backup_dir_rel)
    if not cp_root.is_dir():
        return {"removed_count": 0, "kept_count": 0, "kept_bytes": 0}

    active_resolved = {str(Path(p).resolve()) for p in (active_snapshot_dirs or set()) if p}
    ts = float(now_ts if now_ts is not None else time.time())
    max_age_sec = max(0, max_age_days) * 86400.0

    entries = []
    try:
        children = list(cp_root.iterdir())
    except OSError:
        return {"removed_count": 0, "kept_count": 0, "kept_bytes": 0}

    for entry in children:
        if entry.is_dir():
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            entries.append((mtime, entry.name, entry, _dir_size_bytes(entry)))

    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)

    kept_resolved = 0
    kept_bytes = 0
    removed_count = 0

    for mtime, _name, path_obj, size_b in entries:
        is_active = str(path_obj.resolve()) in active_resolved
        if is_active:
            kept_bytes += size_b
            continue

        too_many = kept_resolved >= max_keep
        too_old = max_age_days > 0 and (ts - mtime) > max_age_sec
        too_big = max_bytes > 0 and (kept_bytes + size_b) > max_bytes

        if too_many or too_old or too_big:
            shutil.rmtree(path_obj, ignore_errors=True)
            removed_count += 1
        else:
            kept_resolved += 1
            kept_bytes += size_b

    return {
        "removed_count": removed_count,
        "kept_count": kept_resolved,
        "kept_bytes": kept_bytes,
    }


def ensure_active_checkpoint(
    db: Any,
    project: Any,
    session_id: str,
    summary: str = "Current state of the project is saved.",
) -> Any:
    """Returns existing active checkpoint for session_id or snapshots project files first."""
    import uuid as _uuid

    existing = db.get_active_checkpoint(session_id=session_id)
    if existing is not None:
        return existing

    cp_id = str(_uuid.uuid4())
    active_dirs = db.list_active_checkpoint_dirs(project.id)
    snap_info = create_checkpoint_snapshot(
        project.root_path,
        project.backup_dir,
        checkpoint_id=cp_id,
        extra_ignores=project.ignore_patterns,
        active_snapshot_dirs=active_dirs,
    )
    after_msg_index = len(db.list_messages(session_id))
    return db.create_checkpoint(
        checkpoint_id=cp_id,
        project_id=project.id,
        session_id=session_id,
        snapshot_dir=snap_info["snapshot_dir"],
        files_count=snap_info["files_count"],
        after_msg_index=after_msg_index,
        summary=summary,
    )


def record_write_in_checkpoint(
    db: Any,
    checkpoint: Any,
    rel_path: str,
    existed_before: bool,
    source_backup_path: Optional[str | Path] = None,
) -> Any:
    """Records a touched or created file on both the checkpoint manifest and SQLite row."""
    if checkpoint is None:
        return None
    record_checkpoint_touch(
        checkpoint.snapshot_dir,
        rel_path,
        existed_before,
        source_backup_path=source_backup_path,
    )
    return db.record_checkpoint_file(checkpoint.id, rel_path, existed_before)


def auto_commit_active_checkpoint_on_tests_pass(
    db: Any,
    project: Any,
    session_id: str,
    command: str,
) -> Any:
    """Replaces/commits the active checkpoint once tests pass cleanly."""
    cp = db.get_active_checkpoint(session_id=session_id)
    if cp is None:
        return None
    committed = db.resolve_checkpoint(
        cp.id,
        "committed",
        f"Tests passed ({command}) — saved state replaced & committed.",
    )
    prune_checkpoints_directory(
        project.root_path,
        project.backup_dir,
        active_snapshot_dirs=set(db.list_active_checkpoint_dirs(project.id)),
    )
    return committed


def discard_checkpoint(
    db: Any,
    project: Any,
    checkpoint_id: str,
) -> bool:
    """Deletes a temporary no-op checkpoint and cleans its snapshot directory if unused."""
    cp = db.get_checkpoint(checkpoint_id) if hasattr(db, "get_checkpoint") else None
    deleted = bool(db.delete_checkpoint(checkpoint_id)) if hasattr(db, "delete_checkpoint") else False
    active_dirs = set(db.list_active_checkpoint_dirs(project.id)) if hasattr(db, "list_active_checkpoint_dirs") else set()
    active_resolved = {str(Path(p).resolve()) for p in active_dirs if p}
    if cp is not None and getattr(cp, "snapshot_dir", None):
        snap_path = Path(cp.snapshot_dir)
        try:
            if snap_path.is_dir() and str(snap_path.resolve()) not in active_resolved:
                shutil.rmtree(snap_path, ignore_errors=True)
        except OSError:
            pass
    prune_checkpoints_directory(
        project.root_path,
        project.backup_dir,
        active_snapshot_dirs=active_dirs,
    )
    return deleted

