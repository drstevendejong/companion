"""
services/patch_tools.py - 3-tier patching (literal, line-range, full write),
Python AST pre-flight check, unified diffs, and project-scoped backup/undo.
"""

from __future__ import annotations

import ast
import difflib
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import (
    BACKUP_MAX_AGE_DAYS,
    BACKUP_MAX_BYTES,
    BACKUP_MAX_SNAPSHOTS,
    BACKUP_MIN_KEEP,
)
from services.fs_tools import resolve_safe_path
from services.token_utils import estimate_tokens


class PatchValidationError(ValueError):
    """Raised when a patch fails uniqueness, line bounds, or Python AST validation."""


def _make_unified_diff(rel_path: str, old_text: str, new_text: str) -> str:
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm="\n",
    )
    return "".join(diff)


def _validate_python_ast(rel_path: str, candidate_content: str) -> None:
    if not rel_path.lower().endswith(".py"):
        return
    try:
        ast.parse(candidate_content, filename=rel_path)
    except SyntaxError as exc:
        raise PatchValidationError(
            f"Python AST syntax error in '{rel_path}' at line {exc.lineno}: {exc.msg}"
        ) from exc


def preview_literal_patch(
    project_root: str | Path,
    rel_path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> Dict[str, Any]:
    """Computes resulting content, unified diff, and AST validity without touching disk."""
    fpath = resolve_safe_path(project_root, rel_path)
    if not fpath.is_file():
        raise FileNotFoundError(f"File not found: {rel_path}")
    original = fpath.read_text(encoding="utf-8")
    count = original.count(old_text)
    if count == 0:
        raise PatchValidationError(f"Target literal block not found in '{rel_path}'.")
    if count > 1 and not replace_all:
        raise PatchValidationError(
            f"Target literal block matched {count} times in '{rel_path}'; provide more surrounding context or set replace_all=True."
        )
    modified = original.replace(old_text, new_text) if replace_all else original.replace(old_text, new_text, 1)
    _validate_python_ast(rel_path, modified)
    diff = _make_unified_diff(rel_path, original, modified)
    return {
        "path": rel_path,
        "existed_before": True,
        "original": original,
        "modified": modified,
        "diff": diff,
        "ast_valid": True,
        "occurrences": count,
        "estimated_tokens": estimate_tokens(diff),
    }


def preview_line_patch(
    project_root: str | Path,
    rel_path: str,
    start_line: int,
    end_line: int,
    new_text: str,
) -> Dict[str, Any]:
    """Computes line-range replacement diff and AST validity without touching disk."""
    fpath = resolve_safe_path(project_root, rel_path)
    if not fpath.is_file():
        raise FileNotFoundError(f"File not found: {rel_path}")
    original = fpath.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    total = len(lines)
    if start_line < 1 or end_line < start_line or start_line > total + 1:
        raise PatchValidationError(
            f"Invalid line range {start_line}-{end_line} for '{rel_path}' ({total} lines)."
        )
    replacement_lines = new_text.splitlines(keepends=True)
    if new_text and not new_text.endswith("\n") and end_line <= total and lines[end_line - 1].endswith("\n"):
        replacement_lines[-1] = replacement_lines[-1] + "\n"

    updated_lines = lines[: start_line - 1] + replacement_lines + lines[end_line:]
    modified = "".join(updated_lines)
    _validate_python_ast(rel_path, modified)
    diff = _make_unified_diff(rel_path, original, modified)
    return {
        "path": rel_path,
        "existed_before": True,
        "original": original,
        "modified": modified,
        "diff": diff,
        "ast_valid": True,
        "start_line": start_line,
        "end_line": end_line,
        "estimated_tokens": estimate_tokens(diff),
    }


def preview_full_write(
    project_root: str | Path,
    rel_path: str,
    content: str,
) -> Dict[str, Any]:
    """Computes full-file write diff and AST validity without touching disk."""
    fpath = resolve_safe_path(project_root, rel_path)
    existed = fpath.exists()
    original = fpath.read_text(encoding="utf-8") if existed and fpath.is_file() else ""
    _validate_python_ast(rel_path, content)
    diff = _make_unified_diff(rel_path, original, content)
    return {
        "path": rel_path,
        "existed_before": existed,
        "original": original,
        "modified": content,
        "diff": diff,
        "ast_valid": True,
        "estimated_tokens": estimate_tokens(diff or content),
    }


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


def prune_backup_directory(
    project_root: str | Path,
    backup_dir_rel: str,
    *,
    max_snapshots: int = BACKUP_MAX_SNAPSHOTS,
    max_age_days: int = BACKUP_MAX_AGE_DAYS,
    max_bytes: int = BACKUP_MAX_BYTES,
    min_keep: int = BACKUP_MIN_KEEP,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Self-cleans old backup snapshots in <project_root>/<backup_dir>/ by enforcing
    count, age, and total disk size limits while always preserving the newest snapshot.
    """
    root = Path(project_root).resolve()
    backup_base = Path(backup_dir_rel)
    if not backup_base.is_absolute():
        backup_base = (root / backup_base).resolve()

    if not backup_base.is_dir():
        return {"removed_count": 0, "kept_count": 0, "kept_bytes": 0}

    ts = float(now_ts if now_ts is not None else time.time())
    max_age_sec = max(0, max_age_days) * 86400.0
    snapshots: List[Tuple[float, str, Path, int]] = []

    try:
        children = list(backup_base.iterdir())
    except OSError:
        return {"removed_count": 0, "kept_count": 0, "kept_bytes": 0}

    for entry in children:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            snapshots.append((mtime, entry.name, entry, _dir_size_bytes(entry)))

    # Newest first (mtime, then timestamp folder name descending)
    snapshots.sort(key=lambda item: (item[0], item[1]), reverse=True)

    kept_count = 0
    kept_bytes = 0
    removed_count = 0

    for idx, (mtime, _name, path_obj, size_b) in enumerate(snapshots):
        must_keep_first = idx == 0
        too_many = idx >= max_snapshots
        too_old = idx >= min_keep and max_age_days > 0 and (ts - mtime) > max_age_sec
        too_big = (not must_keep_first) and max_bytes > 0 and (kept_bytes + size_b) > max_bytes

        if too_many or too_old or too_big:
            shutil.rmtree(path_obj, ignore_errors=True)
            removed_count += 1
        else:
            kept_count += 1
            kept_bytes += size_b

    return {
        "removed_count": removed_count,
        "kept_count": kept_count,
        "kept_bytes": kept_bytes,
    }


def _backup_target_file(
    project_root: str | Path,
    backup_dir_rel: str,
    rel_path: str,
) -> Optional[str]:
    """Copies the pre-patch file into <project_root>/<backup_dir>/<timestamp>/... and self-cleans old snapshots."""
    root = Path(project_root).resolve()
    target = resolve_safe_path(root, rel_path)
    if not target.exists():
        return None

    backup_base = Path(backup_dir_rel)
    if not backup_base.is_absolute():
        backup_base = (root / backup_base).resolve()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    dest = backup_base / stamp / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, dest)
    prune_backup_directory(root, backup_dir_rel)
    return str(dest)


def apply_prepared_patch(
    project_root: str | Path,
    backup_dir_rel: str,
    preview: Dict[str, Any],
) -> Dict[str, Any]:
    """Backs up original file inside project's backup folder and writes modified content."""
    rel_path = preview["path"]
    target = resolve_safe_path(project_root, rel_path)
    backup_path = _backup_target_file(project_root, backup_dir_rel, rel_path) if preview["existed_before"] else None

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(preview["modified"], encoding="utf-8")
    return {
        "path": rel_path,
        "existed_before": preview["existed_before"],
        "backup_path": backup_path,
        "diff": preview["diff"],
        "bytes_written": len(preview["modified"].encode("utf-8")),
    }


def undo_patch_on_disk(
    project_root: str | Path,
    rel_path: str,
    backup_path: Optional[str],
    existed_before: bool,
) -> Dict[str, Any]:
    """Restores a file from its project backup snapshot, or removes newly created file."""
    target = resolve_safe_path(project_root, rel_path)
    if not existed_before:
        if target.exists():
            target.unlink()
        return {"path": rel_path, "action": "removed_new_file"}

    if not backup_path:
        raise FileNotFoundError(f"No backup snapshot recorded for '{rel_path}'.")
    src = Path(backup_path)
    if not src.is_file():
        raise FileNotFoundError(f"Backup snapshot missing on disk: {src}")

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    return {"path": rel_path, "action": "restored_from_backup", "backup_path": str(src)}
