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


def _is_ignored(name: str, rel_posix: str, ignore_set: Set[str], is_dir: bool = False) -> bool:
    if is_dir and name.startswith(".") and name not in (".", ".."):
        return True
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
    max_entries: Optional[int] = None,
) -> Dict[str, Any]:
    """Returns a compact indented tree and structured entry list (untruncated by default)."""
    root = Path(project_root).resolve()
    target = resolve_safe_path(root, subpath)
    if not target.is_dir():
        raise NotADirectoryError(f"Not a directory: {subpath}")

    ignores = _merge_ignores(extra_ignores)
    cap = max_entries if (max_entries is not None and max_entries > 0) else None
    lines: List[str] = []
    entries: List[Dict[str, Any]] = []
    truncated = False

    def _walk(current: Path, depth: int) -> None:
        nonlocal truncated
        if depth > max_depth or (cap is not None and len(entries) >= cap):
            if cap is not None and len(entries) >= cap:
                truncated = True
            return
        try:
            children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return

        for child in children:
            rel_posix = child.relative_to(root).as_posix()
            is_dir = child.is_dir()
            if _is_ignored(child.name, rel_posix, ignores, is_dir=is_dir):
                continue
            indent = "  " * depth
            suffix = "/" if is_dir else f" ({child.stat().st_size} B)"
            lines.append(f"{indent}{child.name}{suffix}")
            entries.append({
                "path": rel_posix,
                "name": child.name,
                "is_dir": is_dir,
                "size": 0 if is_dir else child.stat().st_size,
            })
            if cap is not None and len(entries) >= cap:
                truncated = True
                return
            if is_dir and depth < max_depth:
                _walk(child, depth + 1)

    _walk(target, 0)
    tree_text = "\n".join(lines)
    if truncated and cap is not None:
        tree_text += f"\n... [Tree truncated at {cap} entries]"
    return {
        "subpath": subpath,
        "tree": tree_text,
        "entries": entries,
        "truncated": truncated,
        "estimated_tokens": estimate_tokens(tree_text),
    }


def _expand_globs(glob_expr: str) -> List[str]:
    """Expands brace globs like '*.{py,js,html}' and comma lists like '*.py,*.js'."""
    expanded: List[str] = []
    # Split on commas outside {...}
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    for ch in glob_expr:
        if ch == "{":
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
        elif ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)

    brace_re = re.compile(r"\{([^{}]+)\}")
    queue = parts
    while queue:
        item = queue.pop(0)
        m = brace_re.search(item)
        if m:
            for alt in m.group(1).split(","):
                queue.append(item[: m.start()] + alt.strip() + item[m.end() :])
        elif item:
            expanded.append(item.lstrip("./"))
    return expanded


def _matches_path_glob(rel_posix: str, fname: str, patterns: Sequence[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(fname, pat):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(rel_posix, pat[3:]):
            return True
    return False


def _compact_line_ranges(line_nums: Sequence[int]) -> str:
    """Formats sorted 1-based line numbers compactly, e.g. [10,11,12,15] -> '10-12,15'."""
    if not line_nums:
        return ""
    ranges: List[str] = []
    start = prev = line_nums[0]
    for num in line_nums[1:]:
        if num == prev + 1:
            prev = num
        else:
            ranges.append(f"{start}-{prev}" if prev > start else str(start))
            start = prev = num
    ranges.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(ranges)


def _format_line_excerpt(
    line: str,
    compiled: re.Pattern[str],
    is_hit: bool,
    max_line_chars: int = 320,
    edge_chars: int = 45,
    match_pad: int = 70,
    max_windows_per_line: int = 5,
) -> str:
    """Truncates huge single lines (e.g. minified bundles or embedded JSON) while preserving prefix, match context, and postfix."""
    n = len(line)
    if n <= max_line_chars:
        return line

    if not is_hit:
        head = max(40, min(100, edge_chars * 2))
        tail = max(30, edge_chars)
        omitted = max(0, n - head - tail)
        return f"{line[:head]}...[{omitted} chars]...{line[n - tail:]}"

    intervals: List[tuple[int, int]] = [(0, min(n, edge_chars))]
    match_iter = list(compiled.finditer(line))
    truncated_extra_matches = 0
    if len(match_iter) > max_windows_per_line:
        truncated_extra_matches = len(match_iter) - max_windows_per_line
        match_iter = match_iter[:max_windows_per_line]

    for m in match_iter:
        intervals.append((max(0, m.start() - match_pad), min(n, m.end() + match_pad)))

    intervals.append((max(0, n - edge_chars), n))
    intervals.sort()

    merged: List[tuple[int, int]] = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    parts: List[str] = []
    for idx, (s, e) in enumerate(merged):
        if idx > 0:
            gap = s - merged[idx - 1][1]
            if gap > 0:
                parts.append(f"...[{gap} chars]...")
        parts.append(line[s:e])

    out = "".join(parts)
    if truncated_extra_matches > 0:
        out += f" ...[{truncated_extra_matches} more matches on line]"
    return out


def search_code(
    project_root: str | Path,
    pattern: str,
    is_regex: bool = False,
    path_glob: Optional[str] = None,
    context_lines: int = 2,
    max_matches: Optional[int] = None,
    extra_ignores: Optional[Sequence[str]] = None,
    ignore_case: bool = True,
) -> Dict[str, Any]:
    """Searches any text files (and matching filenames) across the project with deduplicated hunks."""
    root = Path(project_root).resolve()
    ignores = _merge_ignores(extra_ignores)
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    compiled = re.compile(pattern if is_regex else re.escape(pattern), flags)
    glob_patterns = _expand_globs(path_glob) if path_glob and path_glob.strip() else []
    cap = max_matches if (max_matches is not None and max_matches > 0) else None
    ctx = max(0, context_lines)

    matches: List[Dict[str, Any]] = []
    formatted_blocks: List[str] = []
    total_hits = 0

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
            if glob_patterns and not _matches_path_glob(rel_posix, fname, glob_patterns):
                continue
            try:
                if fpath.stat().st_size > 1_500_000:
                    continue
                raw_bytes = fpath.read_bytes()
            except OSError:
                continue

            # Report filename/path matches even before scanning contents
            if compiled.search(rel_posix):
                total_hits += 1
                if cap is None or len(matches) < cap:
                    fname_snippet = f"[Filename match: {rel_posix}]"
                    matches.append({
                        "path": rel_posix,
                        "line": 0,
                        "snippet": fname_snippet,
                    })
                    formatted_blocks.append(f"--- {rel_posix} ---\n{fname_snippet}")

            # Skip binary files (NUL byte in header), decode text permissively (UTF-8 / Latin-1 fallback)
            if b"\x00" in raw_bytes[:4096]:
                continue
            raw = raw_bytes.decode("utf-8", errors="replace")

            file_lines = raw.splitlines()
            matched_indices = [idx for idx, line in enumerate(file_lines) if compiled.search(line)]
            if not matched_indices:
                continue

            included_indices: List[int] = []
            for idx in matched_indices:
                total_hits += 1
                if cap is None or len(matches) < cap:
                    included_indices.append(idx)
                    matches.append({
                        "path": rel_posix,
                        "line": idx + 1,
                        "snippet": _format_line_excerpt(file_lines[idx], compiled, is_hit=True),
                    })

            if not included_indices:
                continue

            # Merge overlapping or adjacent context windows inside this file into compact hunks
            hunks: List[Dict[str, Any]] = []
            for idx in included_indices:
                w_start = max(0, idx - ctx)
                w_end = min(len(file_lines) - 1, idx + ctx)
                if hunks and w_start <= hunks[-1]["end"] + 1:
                    hunks[-1]["end"] = max(hunks[-1]["end"], w_end)
                    hunks[-1]["hits"].append(idx)
                else:
                    hunks.append({"start": w_start, "end": w_end, "hits": [idx]})

            for h in hunks:
                hit_set = set(h["hits"])
                hit_label = _compact_line_ranges([i + 1 for i in h["hits"]])
                non_empty_lines: List[str] = []
                first_kept_line: Optional[int] = None
                last_kept_line: Optional[int] = None
                for li in range(h["start"], h["end"] + 1):
                    raw_line = file_lines[li]
                    is_hit = li in hit_set
                    if not raw_line.strip() and not is_hit:
                        continue
                    if first_kept_line is None:
                        first_kept_line = li + 1
                    last_kept_line = li + 1
                    non_empty_lines.append(_format_line_excerpt(raw_line, compiled, is_hit=is_hit))

                if ctx > 0 and first_kept_line is not None and last_kept_line is not None:
                    span_label = f"{first_kept_line}-{last_kept_line}" if last_kept_line > first_kept_line else str(first_kept_line)
                    header = f"--- {rel_posix}:{hit_label} (L{span_label}) ---"
                else:
                    header = f"--- {rel_posix}:{hit_label} ---"
                formatted_blocks.append(header + "\n" + "\n".join(non_empty_lines))

    summary_text = "\n\n".join(formatted_blocks) if formatted_blocks else "No matches found."
    if cap is not None and total_hits > cap:
        summary_text += f"\n\n... [{total_hits - cap} more matches omitted; narrow path_glob or pattern]"

    return {
        "pattern": pattern,
        "is_regex": is_regex,
        "ignore_case": ignore_case,
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
