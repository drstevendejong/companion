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


def _find_line_occurrences(original: str, old_text: str) -> List[int]:
    lines_out: List[int] = []
    start = 0
    while True:
        idx = original.find(old_text, start)
        if idx < 0:
            break
        lines_out.append(original[:idx].count("\n") + 1)
        start = idx + max(1, len(old_text))
    return lines_out


def _build_literal_not_found_diagnostic(
    rel_path: str,
    original: str,
    old_text: str,
    has_crlf_on_disk: bool = False,
) -> str:
    base_msg = f"Target literal block not found in '{rel_path}'."
    orig_lines = original.splitlines()
    old_lines = old_text.splitlines()
    if not orig_lines or not old_lines:
        return base_msg

    # 1. Exact line-sequence match when ignoring trailing or leading+trailing whitespace
    old_len = len(old_lines)
    for mode_label, norm_fn in (
        ("trailing whitespace", lambda s: s.rstrip()),
        ("indentation/whitespace", lambda s: s.strip()),
    ):
        norm_old = [norm_fn(ln) for ln in old_lines]
        if not any(norm_old):
            continue
        norm_orig = [norm_fn(ln) for ln in orig_lines]
        for i in range(0, len(norm_orig) - old_len + 1):
            if norm_orig[i : i + old_len] == norm_old:
                s_line = i + 1
                e_line = i + old_len
                actual_slice = orig_lines[i : i + old_len]
                numbered = "\n".join(
                    f"{s_line + k}: {actual_slice[k]}" for k in range(min(len(actual_slice), 25))
                )
                if len(actual_slice) > 25:
                    numbered += "\n... [more lines omitted]"
                crlf_hint = " (file uses CRLF line endings on disk)" if has_crlf_on_disk else ""
                return (
                    f"{base_msg} Found matching lines at {s_line}–{e_line} when ignoring {mode_label}{crlf_hint}.\n"
                    f"Actual lines {s_line}–{e_line}:\n{numbered}"
                )

    # 2. Fast candidate-anchored sliding window search via difflib
    line_to_indices: Dict[str, List[int]] = {}
    for idx, ln in enumerate(orig_lines):
        key = ln.strip()
        if len(key) >= 4:
            bucket = line_to_indices.setdefault(key, [])
            if len(bucket) < 40:
                bucket.append(idx)

    candidate_starts: set[int] = set()
    for offset, old_ln in enumerate(old_lines):
        key = old_ln.strip()
        for hit_idx in line_to_indices.get(key, ()):
            cand_start = max(0, hit_idx - offset)
            if cand_start < len(orig_lines):
                candidate_starts.add(cand_start)

    if not candidate_starts and len(orig_lines) <= 250:
        step = max(1, old_len // 2)
        candidate_starts.update(range(0, len(orig_lines), step))

    best_ratio = 0.0
    best_span: Optional[Tuple[int, int]] = None
    old_joined = "\n".join(old_lines)

    for s_idx in sorted(candidate_starts):
        e_idx = min(len(orig_lines), s_idx + old_len)
        cand_lines = orig_lines[s_idx:e_idx]
        if not cand_lines:
            continue
        ratio = difflib.SequenceMatcher(None, old_joined, "\n".join(cand_lines)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_span = (s_idx, e_idx)

    if best_span is not None and best_ratio >= 0.42:
        s_idx, e_idx = best_span
        ctx_s = max(0, s_idx - 1)
        ctx_e = min(len(orig_lines), e_idx + 1)
        actual_lines = orig_lines[ctx_s:ctx_e]
        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                orig_lines[s_idx:e_idx],
                fromfile="expected (old_text)",
                tofile=f"actual ({rel_path}:{s_idx + 1}-{e_idx})",
                lineterm="",
            )
        )
        diff_snippet = "\n".join(diff_lines[:32])
        if len(diff_lines) > 32:
            diff_snippet += "\n... [diff truncated]"
        numbered = "\n".join(
            f"{ctx_s + 1 + k}: {actual_lines[k]}" for k in range(min(len(actual_lines), 22))
        )
        if len(actual_lines) > 22:
            numbered += "\n... [more lines omitted]"
        return (
            f"{base_msg}\n"
            f"Nearest match around lines {s_idx + 1}–{e_idx} ({int(best_ratio * 100)}% similar):\n"
            f"{diff_snippet}\n"
            f"Actual lines {ctx_s + 1}–{ctx_e}:\n{numbered}"
        )

    return base_msg


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
    raw_bytes = fpath.read_bytes()
    has_crlf = b"\r\n" in raw_bytes
    original = raw_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    norm_old = old_text.replace("\r\n", "\n").replace("\r", "\n")
    norm_new = new_text.replace("\r\n", "\n").replace("\r", "\n")
    count = original.count(norm_old)
    if count == 0:
        raise PatchValidationError(
            _build_literal_not_found_diagnostic(rel_path, original, norm_old, has_crlf_on_disk=has_crlf)
        )
    if count > 1 and not replace_all:
        occ_lines = _find_line_occurrences(original, norm_old)
        occ_note = (
            f" (starting at lines {', '.join(str(n) for n in occ_lines[:8])}"
            + (", ..." if len(occ_lines) > 8 else "")
            + ")"
            if occ_lines
            else ""
        )
        raise PatchValidationError(
            f"Target literal block matched {count} times in '{rel_path}'{occ_note}; provide more surrounding context or set replace_all=True."
        )
    modified = original.replace(norm_old, norm_new) if replace_all else original.replace(norm_old, norm_new, 1)
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


def preview_delete_file(
    project_root: str | Path,
    rel_path: str,
) -> Dict[str, Any]:
    """Computes deletion preview diff without removing the file from disk."""
    fpath = resolve_safe_path(project_root, rel_path)
    if not fpath.exists():
        raise FileNotFoundError(f"File not found: {rel_path}")
    if fpath.is_dir():
        raise PatchValidationError(f"Cannot delete directory '{rel_path}'; delete_file supports files only.")

    size_bytes = fpath.stat().st_size
    if size_bytes <= 200_000:
        raw_bytes = fpath.read_bytes()
        if b"\x00" in raw_bytes[:4096]:
            diff = f"--- a/{rel_path}\n+++ /dev/null\n@@ delete binary file ({size_bytes:,} bytes) @@\n"
        else:
            original = raw_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n")
            orig_lines = original.splitlines(keepends=True)
            diff = "".join(
                difflib.unified_diff(
                    orig_lines,
                    [],
                    fromfile=f"a/{rel_path}",
                    tofile="/dev/null",
                    lineterm="\n",
                )
            ) or f"--- a/{rel_path}\n+++ /dev/null\n@@ delete empty file @@\n"
    else:
        diff = f"--- a/{rel_path}\n+++ /dev/null\n@@ delete large file ({size_bytes:,} bytes) @@\n"

    return {
        "action": "delete",
        "path": rel_path,
        "existed_before": True,
        "size_bytes": size_bytes,
        "diff": diff,
        "ast_valid": None,
        "estimated_tokens": min(250, max(15, estimate_tokens(diff))),
    }


def apply_delete_file(
    project_root: str | Path,
    backup_dir_rel: str,
    preview: Dict[str, Any],
) -> Dict[str, Any]:
    """Backs up the target file into backup/ before deleting it from disk."""
    rel_path = preview["path"]
    target = resolve_safe_path(project_root, rel_path)
    if not target.is_file():
        raise FileNotFoundError(f"File not found: {rel_path}")
    backup_path = _backup_target_file(project_root, backup_dir_rel, rel_path)
    target.unlink()
    return {
        "path": rel_path,
        "existed_before": True,
        "backup_path": backup_path,
        "diff": preview["diff"],
        "deleted": True,
    }


def preview_rename_file(
    project_root: str | Path,
    old_path: str,
    new_path: str,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Validates and previews a safe file rename/move inside project_root."""
    root = Path(project_root).resolve()
    src = resolve_safe_path(root, old_path)
    dst = resolve_safe_path(root, new_path)

    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {old_path}")
    if src.is_dir():
        raise PatchValidationError(f"Cannot rename directory '{old_path}'; rename_file supports files only.")
    if src == dst:
        raise PatchValidationError(f"Source and destination paths are identical: '{old_path}'.")
    if dst.is_dir():
        raise PatchValidationError(f"Destination '{new_path}' is an existing directory.")
    if dst.exists() and not overwrite:
        raise PatchValidationError(f"Destination '{new_path}' already exists (pass overwrite=true to replace).")

    src_rel = src.relative_to(root).as_posix()
    dst_rel = dst.relative_to(root).as_posix()
    size_bytes = src.stat().st_size
    note = " (overwriting existing destination)" if dst.exists() else ""
    diff = (
        f"--- a/{src_rel}\n"
        f"+++ b/{dst_rel}\n"
        f"@@ rename {src_rel} -> {dst_rel} ({size_bytes:,} bytes){note} @@\n"
    )
    return {
        "action": "rename",
        "path": src_rel,
        "old_path": src_rel,
        "new_path": dst_rel,
        "overwrite": bool(overwrite),
        "existed_before": True,
        "dest_existed_before": dst.exists(),
        "size_bytes": size_bytes,
        "diff": diff,
        "ast_valid": None,
        "estimated_tokens": max(20, estimate_tokens(diff)),
    }


def apply_rename_file(
    project_root: str | Path,
    backup_dir_rel: str,
    preview: Dict[str, Any],
) -> Dict[str, Any]:
    """Backs up the source (and overwritten destination, if any) before moving on disk."""
    root = Path(project_root).resolve()
    old_path = preview["old_path"]
    new_path = preview["new_path"]
    overwrite = bool(preview.get("overwrite", False))

    src = resolve_safe_path(root, old_path)
    dst = resolve_safe_path(root, new_path)
    if not src.is_file():
        raise FileNotFoundError(f"Source file not found: {old_path}")
    if dst.is_dir():
        raise PatchValidationError(f"Destination '{new_path}' is an existing directory.")
    if dst.exists() and not overwrite:
        raise PatchValidationError(f"Destination '{new_path}' already exists.")

    backup_path = _backup_target_file(root, backup_dir_rel, old_path)
    dest_existed = dst.is_file()
    if dest_existed:
        _backup_target_file(root, backup_dir_rel, new_path)

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {
        "path": old_path,
        "old_path": old_path,
        "new_path": new_path,
        "existed_before": True,
        "dest_existed_before": dest_existed,
        "backup_path": backup_path,
        "diff": preview["diff"],
        "renamed": True,
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
