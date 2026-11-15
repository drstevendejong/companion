"""
services/fs_tools.py - Sandboxed filesystem inspection: tree, regex/substring probe, line slice, full read.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from config import DEFAULT_IGNORE_PATTERNS
from services.token_utils import estimate_tokens


class PathSandboxError(ValueError):
    """Raised when a path attempts to escape the configured project root."""


def resolve_safe_path(project_root: str | Path, rel_path: str = ".") -> Path:
    """Resolves rel_path strictly inside project_root."""
    root = Path(project_root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Project root does not exist: {root}")
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PathSandboxError(f"Path '{rel_path}' escapes project root '{root}'.") from exc
    return candidate


def _is_ignored(name: str, rel_posix: str, ignore_set: Set[str]) -> bool:
    for pattern in ignore_set:
        if name == pattern or fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel_posix, pattern):
            return True
    return False


def _merge_ignores(extra_ignores: Optional[Sequence[str]]) -> Set[str]:
    merged = set(DEFAULT_IGNORE_PATTERNS)
    if extra_ignores:
        for pat in extra_ignores:
            if pat and pat.strip():
                merged.add(pat.strip())
    return merged


def list_tree(
    project_root: str | Path,
    subpath: str = ".",
    max_depth: int = 3,
    extra_ignores: Optional[Sequence[str]] = None,
    max_entries: int = 300,
) -> Dict[str, Any]:
    """Returns a compact indented tree and structured entry list."""
    root = Path(project_root).resolve()
    target = resolve_safe_path(root, subpath)
    if not target.is_dir():
        raise NotADirectoryError(f"Not a directory: {subpath}")

    ignores = _merge_ignores(extra_ignores)
    lines: List[str] = []
    entries: List[Dict[str, Any]] = []
    truncated = False

    def _walk(current: Path, depth: int) -> None:
        nonlocal truncated
        if depth > max_depth or len(entries) >= max_entries:
            if len(entries) >= max_entries:
                truncated = True
            return
        try:
            children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return

        for child in children:
            rel_posix = child.relative_to(root).as_posix()
            if _is_ignored(child.name, rel_posix, ignores):
                continue
            indent = "  " * depth
            is_dir = child.is_dir()
            suffix = "/" if is_dir else f" ({child.stat().st_size} B)"
            lines.append(f"{indent}{child.name}{suffix}")
            entries.append({
                "path": rel_posix,
                "name": child.name,
                "is_dir": is_dir,
                "size": 0 if is_dir else child.stat().st_size,
            })
            if len(entries) >= max_entries:
                truncated = True
                return
            if is_dir and depth < max_depth:
                _walk(child, depth + 1)

    _walk(target, 0)
    tree_text = "\n".join(lines)
    if truncated:
        tree_text += f"\n... [Tree truncated at {max_entries} entries]"
    return {
        "subpath": subpath,
        "tree": tree_text,
        "entries": entries,
        "truncated": truncated,
        "estimated_tokens": estimate_tokens(tree_text),
    }


def search_code(
    project_root: str | Path,
    pattern: str,
    is_regex: bool = False,
    path_glob: Optional[str] = None,
    context_lines: int = 2,
    max_matches: int = 40,
    extra_ignores: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Searches text files for substring or regex matches with ±N context lines."""
    root = Path(project_root).resolve()
    ignores = _merge_ignores(extra_ignores)
    flags = re.MULTILINE
    compiled = re.compile(pattern if is_regex else re.escape(pattern), flags)

    matches: List[Dict[str, Any]] = []
    total_hits = 0

    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath)
        rel_dir = cur.relative_to(root).as_posix()
        dirnames[:] = sorted(
            d for d in dirnames
            if not _is_ignored(d, f"{rel_dir}/{d}".lstrip("./"), ignores)
        )
        for fname in sorted(filenames):
            fpath = cur / fname
            rel_posix = fpath.relative_to(root).as_posix()
            if _is_ignored(fname, rel_posix, ignores):
                continue
            if path_glob and not fnmatch.fnmatch(rel_posix, path_glob) and not fnmatch.fnmatch(fname, path_glob):
                continue
            if fpath.stat().st_size > 1_500_000:
                continue
            try:
                raw = fpath.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            file_lines = raw.splitlines()
            for idx, line in enumerate(file_lines):
                if compiled.search(line):
                    total_hits += 1
                    if len(matches) < max_matches:
                        start_ctx = max(0, idx - context_lines)
                        end_ctx = min(len(file_lines), idx + context_lines + 1)
                        snippet_lines = [
                            f"{n + 1}: {file_lines[n]}"
                            for n in range(start_ctx, end_ctx)
                        ]
                        matches.append({
                            "path": rel_posix,
                            "line": idx + 1,
                            "snippet": "\n".join(snippet_lines),
                        })

    formatted_blocks = [
        f"--- {m['path']}:{m['line']} ---\n{m['snippet']}" for m in matches
    ]
    summary_text = "\n\n".join(formatted_blocks) if formatted_blocks else "No matches found."
    if total_hits > max_matches:
        summary_text += f"\n\n... [{total_hits - max_matches} more matches omitted; narrow path_glob or pattern]"

    return {
        "pattern": pattern,
        "is_regex": is_regex,
        "total_hits": total_hits,
        "shown_hits": len(matches),
        "matches": matches,
        "formatted": summary_text,
        "estimated_tokens": estimate_tokens(summary_text),
    }


def read_lines(
    project_root: str | Path,
    rel_path: str,
    start_line: int = 1,
    end_line: int = 200,
) -> Dict[str, Any]:
    """Reads an inclusive 1-based line range with line numbers."""
    fpath = resolve_safe_path(project_root, rel_path)
    if not fpath.is_file():
        raise FileNotFoundError(f"File not found: {rel_path}")
    text = fpath.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    total_lines = len(all_lines)

    s = max(1, start_line)
    e = min(total_lines, max(s, end_line)) if total_lines > 0 else 0
    selected = [f"{i}: {all_lines[i - 1]}" for i in range(s, e + 1)]
    numbered = "\n".join(selected)
    return {
        "path": rel_path,
        "start_line": s,
        "end_line": e,
        "total_lines": total_lines,
        "content": numbered,
        "raw_slice": "\n".join(all_lines[s - 1 : e]) if e >= s else "",
        "estimated_tokens": estimate_tokens(numbered),
    }


def read_file(
    project_root: str | Path,
    rel_path: str,
    max_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Reads an entire file as raw text (no line-number overhead, no truncation unless max_chars is set)."""
    fpath = resolve_safe_path(project_root, rel_path)
    if not fpath.is_file():
        raise FileNotFoundError(f"File not found: {rel_path}")
    raw = fpath.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    truncated = False
    content = raw
    if max_chars is not None and len(raw) > max_chars:
        content = raw[:max_chars] + f"\n\n... [Truncated at {max_chars} chars; use read_lines for specific sections]"
        truncated = True
    return {
        "path": rel_path,
        "total_lines": len(lines),
        "char_count": len(raw),
        "truncated": truncated,
        "content": content,
        "raw_content": raw,
        "estimated_tokens": estimate_tokens(content),
    }
