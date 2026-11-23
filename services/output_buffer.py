"""
services/output_buffer.py - Thread-safe rolling buffer for raw command/test outputs.

Stores full raw command outputs in memory keyed by short `exec_id` so Companion can return
concise AI summaries or excerpts while still letting the main AI call `read_command_output`
to inspect specific line ranges or regex/substring matches when needed.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from config import OUTPUT_BUFFER_MAX_ENTRIES
from services.token_utils import estimate_tokens

_LOCK = threading.Lock()
_BUFFER: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def record_command_output(
    command: str,
    raw_output: str,
    *,
    exit_code: int = 0,
    duration_ms: int = 0,
    source: str = "gate",
) -> str:
    """Stores a command's raw output in the rolling buffer and returns its exec_id."""
    exec_id = uuid.uuid4().hex[:8]
    normalized = (raw_output or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    entry = {
        "exec_id": exec_id,
        "command": str(command or ""),
        "exit_code": int(exit_code),
        "duration_ms": int(duration_ms or 0),
        "source": str(source or "gate"),
        "output": normalized,
        "total_lines": len(lines),
        "char_count": len(normalized),
        "created_at": time.time(),
    }
    with _LOCK:
        _BUFFER[exec_id] = entry
        _BUFFER.move_to_end(exec_id)
        while len(_BUFFER) > OUTPUT_BUFFER_MAX_ENTRIES:
            _BUFFER.popitem(last=False)
    return exec_id


def get_command_output_entry(exec_id: Optional[str] = None) -> Dict[str, Any]:
    """Retrieves a buffered entry by exec_id, or the most recent entry if exec_id is omitted."""
    with _LOCK:
        if not _BUFFER:
            raise KeyError("No buffered command output is available yet.")
        target = (exec_id or "").strip()
        if not target:
            latest_key = next(reversed(_BUFFER))
            return dict(_BUFFER[latest_key])
        if target not in _BUFFER:
            raise KeyError(
                f"Buffered command output '{target}' not found (it may have expired from the last {OUTPUT_BUFFER_MAX_ENTRIES} runs)."
            )
        return dict(_BUFFER[target])


def read_command_output(
    exec_id: Optional[str] = None,
    *,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    pattern: Optional[str] = None,
    tail_lines: Optional[int] = None,
    ignore_case: bool = True,
    is_regex: bool = False,
    context_lines: int = 2,
    max_chars: int = 18_000,
) -> Dict[str, Any]:
    """Reads a targeted line slice, tail slice, or pattern matches from a buffered command output."""
    try:
        entry = get_command_output_entry(exec_id)
    except KeyError as exc:
        return {
            "found": False,
            "exec_id": (exec_id or "").strip() or "latest",
            "error": str(exc.args[0] if exc.args else exc),
            "estimated_tokens": 0,
        }

    text = entry["output"]
    lines = text.splitlines()
    total_lines = len(lines)
    passed = int(entry["exit_code"]) == 0

    pat = (pattern or "").strip()
    if pat:
        flags = re.IGNORECASE if ignore_case else 0
        if is_regex:
            try:
                regex = re.compile(pat, flags)
            except re.error as exc:
                raise ValueError(f"Invalid regex '{pat}': {exc}") from exc
        else:
            regex = re.compile(re.escape(pat), flags)

        ctx = max(0, min(10, int(context_lines)))
        matched_indices: List[int] = [i for i, ln in enumerate(lines) if regex.search(ln)]
        snippets: List[str] = []
        used_ranges: List[tuple[int, int]] = []
        shown_line_nos: set[int] = set()
        for idx in matched_indices:
            s = max(0, idx - ctx)
            e = min(total_lines, idx + ctx + 1)
            if used_ranges and s <= used_ranges[-1][1]:
                used_ranges[-1] = (used_ranges[-1][0], max(used_ranges[-1][1], e))
            else:
                used_ranges.append((s, e))

        for s, e in used_ranges:
            for ln_no in range(s + 1, e + 1):
                shown_line_nos.add(ln_no)
            block = "\n".join(f"{ln_no}: {lines[ln_no - 1]}" for ln_no in range(s + 1, e + 1))
            snippets.append(block)
        combined = "\n...\n".join(snippets) if snippets else "(No lines matched pattern)"
        truncated = False
        if len(combined) > max_chars:
            combined = combined[:max_chars] + "\n... [output truncated]"
            truncated = True
        return {
            "found": True,
            "exec_id": entry["exec_id"],
            "command": entry["command"],
            "exit_code": entry["exit_code"],
            "passed": passed,
            "mode": "pattern",
            "total_lines": total_lines,
            "shown_lines": len(shown_line_nos),
            "pattern": pat,
            "match_count": len(matched_indices),
            "content": combined,
            "truncated": truncated,
            "estimated_tokens": estimate_tokens(combined),
        }

    if total_lines == 0:
        return {
            "found": True,
            "exec_id": entry["exec_id"],
            "command": entry["command"],
            "exit_code": entry["exit_code"],
            "passed": passed,
            "mode": "tail" if tail_lines is not None else "range",
            "total_lines": 0,
            "shown_lines": 0,
            "start_line": 1,
            "end_line": 0,
            "content": "(Command produced no output)",
            "truncated": False,
            "estimated_tokens": 5,
        }

    if tail_lines is not None and start_line is None and end_line is None:
        t_cnt = max(1, min(total_lines, int(tail_lines)))
        s_line = total_lines - t_cnt + 1
        e_line = total_lines
        mode = "tail"
    else:
        s_line = max(1, min(total_lines, int(start_line or 1)))
        default_end = s_line + 149
        e_line = int(end_line) if end_line is not None else default_end
        e_line = max(s_line, min(total_lines, e_line))
        mode = "range"

    sliced = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(s_line, e_line + 1))
    shown_lines = e_line - s_line + 1

    truncated = False
    if len(sliced) > max_chars:
        sliced = sliced[:max_chars] + "\n... [slice truncated; request a narrower line range]"
        truncated = True

    return {
        "found": True,
        "exec_id": entry["exec_id"],
        "command": entry["command"],
        "exit_code": entry["exit_code"],
        "passed": passed,
        "mode": mode,
        "total_lines": total_lines,
        "shown_lines": shown_lines,
        "start_line": s_line,
        "end_line": e_line,
        "content": sliced,
        "truncated": truncated,
        "estimated_tokens": estimate_tokens(sliced),
    }


def clear_buffer() -> None:
    """Clears the rolling command output buffer (for deterministic tests)."""
    with _LOCK:
        _BUFFER.clear()
