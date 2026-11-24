"""
services/runner.py - Project-scoped command execution using target venv Python when detected.
"""

from __future__ import annotations

import asyncio
import codecs
import inspect
import json
import os
import shlex
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import COMMAND_TIMEOUT_SECONDS, MAX_COMMAND_OUTPUT_CHARS
from services.token_utils import estimate_tokens


def resolve_project_python(project_root: str | Path, configured_python: Optional[str] = None) -> str:
    """Resolves explicit python_path, or auto-detects .venv/venv inside target project."""
    if configured_python and configured_python.strip():
        return configured_python.strip()

    root = Path(project_root).resolve()
    candidates = [
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        root / "venv" / "Scripts" / "python.exe",
        root / "venv" / "bin" / "python",
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return sys.executable


def _prepare_argv(command: str, python_exe: str) -> List[str]:
    tokens = shlex.split(command, posix=(os.name != "nt"))
    if not tokens:
        raise ValueError("Empty command.")
    if tokens[0] in ("python", "python3"):
        tokens[0] = python_exe
    return tokens


async def _emit_callback(cb: Optional[Callable[[str], Any]], text: str) -> None:
    if cb is None or not text:
        return
    try:
        res = cb(text)
        if inspect.isawaitable(res):
            await res
    except Exception:
        pass


async def broadcast_exec_event(app: Any, payload: Dict[str, Any]) -> None:
    """Broadcasts live command execution updates to connected UI lifecycle WebSockets."""
    state_obj = getattr(app, "state", None)
    sockets = getattr(state_obj, "lifecycle_sockets", None) if state_obj is not None else None
    if not sockets:
        return
    raw = json.dumps(payload)
    for ws in list(sockets):
        try:
            await ws.send_text(raw)
        except Exception:
            pass


async def run_project_command(
    project_root: str | Path,
    command: str,
    configured_python: Optional[str] = None,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
    on_output: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Executes a command inside project_root, streaming stdout/stderr chunks + token estimate."""
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Project root directory not found: {root}")

    python_exe = resolve_project_python(root, configured_python)
    argv = _prepare_argv(command, python_exe)

    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"
    child_env.setdefault("PYTHONIOENCODING", "utf-8")

    started = time.monotonic()
    deadline = started + timeout_seconds
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(root),
        env=child_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    chunks: List[str] = []
    timed_out = False

    try:
        while proc.stdout is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                raw_bytes = await asyncio.wait_for(proc.stdout.read(2048), timeout=remaining)
            except asyncio.TimeoutError:
                timed_out = True
                break
            if not raw_bytes:
                break
            text = decoder.decode(raw_bytes, final=False)
            if text:
                chunks.append(text)
                await _emit_callback(on_output, text)

        if timed_out:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            if proc.stdout is not None:
                try:
                    leftover = await asyncio.wait_for(proc.stdout.read(), timeout=2.0)
                    if leftover:
                        tail_txt = decoder.decode(leftover, final=False)
                        if tail_txt:
                            chunks.append(tail_txt)
                            await _emit_callback(on_output, tail_txt)
                except Exception:
                    pass
            await proc.wait()
        else:
            remaining = max(0.5, deadline - time.monotonic())
            try:
                await asyncio.wait_for(proc.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()

        flushed = decoder.decode(b"", final=True)
        if flushed:
            chunks.append(flushed)
            await _emit_callback(on_output, flushed)
    finally:
        if proc.returncode is None and timed_out:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass

    elapsed_ms = int((time.monotonic() - started) * 1000)
    output = "".join(chunks)
    truncated = False
    if len(output) > MAX_COMMAND_OUTPUT_CHARS:
        output = output[-MAX_COMMAND_OUTPUT_CHARS:]
        truncated = True

    exit_code = -1 if timed_out else (proc.returncode if proc.returncode is not None else 0)
    from services import output_buffer

    exec_id = output_buffer.record_command_output(
        command,
        output,
        exit_code=exit_code,
        duration_ms=elapsed_ms,
    )

    return {
        "exec_id": exec_id,
        "command": command,
        "resolved_argv": argv,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": elapsed_ms,
        "output": output,
        "truncated": truncated,
        "estimated_tokens": estimate_tokens(output),
    }


async def stream_project_command(
    app: Any,
    project_root: str | Path,
    command: str,
    configured_python: Optional[str] = None,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
    source: str = "console",
) -> Dict[str, Any]:
    """Runs a project command while broadcasting live start/chunk/done events to UI WebSockets."""
    run_id = uuid.uuid4().hex[:10]
    await broadcast_exec_event(
        app,
        {
            "type": "exec_start",
            "run_id": run_id,
            "command": command,
            "source": source,
        },
    )

    async def _on_chunk(chunk: str) -> None:
        await broadcast_exec_event(
            app,
            {
                "type": "exec_chunk",
                "run_id": run_id,
                "chunk": chunk,
                "source": source,
            },
        )

    try:
        res = await run_project_command(
            project_root,
            command,
            configured_python=configured_python,
            timeout_seconds=timeout_seconds,
            on_output=_on_chunk,
        )
    except Exception as exc:
        await broadcast_exec_event(
            app,
            {
                "type": "exec_done",
                "run_id": run_id,
                "command": command,
                "source": source,
                "exit_code": -1,
                "duration_ms": 0,
                "estimated_tokens": 0,
                "timed_out": False,
                "output": str(exc),
                "error": str(exc),
            },
        )
        raise

    await broadcast_exec_event(
        app,
        {
            "type": "exec_done",
            "run_id": run_id,
            "command": command,
            "source": source,
            "exit_code": res["exit_code"],
            "duration_ms": res["duration_ms"],
            "estimated_tokens": res["estimated_tokens"],
            "timed_out": res["timed_out"],
            "output": res["output"],
        },
    )
    return res


def _quick_check_command_for_target(
    rel_posix: str,
    suffix: str,
    post_cmd: str,
    fast_cmd: str,
    *,
    fast: bool,
) -> Optional[Tuple[str, str]]:
    """Builds a safe quick-check or targeted-test command for a verified relative path."""
    import shlex as _shlex

    q = _shlex.quote(rel_posix)
    fname = Path(rel_posix).name.lower()
    is_py_test = suffix == ".py" and (fname.startswith("test_") or fname.endswith("_test.py"))

    if fast_cmd and "{target}" in fast_cmd:
        return fast_cmd.replace("{target}", q), f"Quick check template for {rel_posix}"

    if suffix == ".py":
        if is_py_test and not fast:
            lower_post = post_cmd.lower()
            if "pytest" in lower_post:
                prefix = "python -m pytest" if "-m pytest" in lower_post else "pytest"
                return f"{prefix} {q}", f"Targeted pytest run for {rel_posix}"
            if "unittest" in lower_post:
                return f"python -m unittest {q} -v", f"Targeted unittest run for {rel_posix}"
        return f"python -m py_compile {q}", f"Python syntax/compile check for {rel_posix}"

    if suffix in (".js", ".mjs", ".cjs"):
        return f"node --check {q}", f"JavaScript syntax check for {rel_posix}"

    if suffix == ".json":
        code = (
            "import json, sys; "
            "json.load(open(sys.argv[1], encoding='utf-8')); "
            "print('JSON OK:', sys.argv[1])"
        )
        return f"python -c {_shlex.quote(code)} {q}", f"JSON syntax check for {rel_posix}"

    if suffix in (".html", ".htm"):
        code = (
            "import sys; from html.parser import HTMLParser; "
            "p = HTMLParser(); "
            "p.feed(open(sys.argv[1], encoding='utf-8').read()); p.close(); "
            "print('HTML OK:', sys.argv[1])"
        )
        return f"python -c {_shlex.quote(code)} {q}", f"HTML parse check for {rel_posix}"

    if suffix == ".css":
        code = (
            "import sys; txt = open(sys.argv[1], encoding='utf-8').read(); "
            "d = 0\n"
            "for ch in txt:\n"
            "  d += (ch == '{') - (ch == '}')\n"
            "  if d < 0: raise SystemExit('Unmatched } in ' + sys.argv[1])\n"
            "if d != 0: raise SystemExit('Unclosed { in ' + sys.argv[1])\n"
            "print('CSS OK:', sys.argv[1])"
        )
        return f"python -c {_shlex.quote(code)} {q}", f"CSS brace/UTF-8 check for {rel_posix}"

    return None


def resolve_test_command(
    project: Any,
    *,
    fast: bool = False,
    target: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Resolves the exact human-gated command for run_tests.
    - Default (fast=False, target=None): runs full project.post_patch_command (auto-commits checkpoint on pass).
    - Targeted or fast: runs project.fast_test_command or safe per-file check (keeps checkpoint active).
    """
    from services.fs_tools import resolve_safe_path

    post_cmd = (getattr(project, "post_patch_command", None) or "").strip()
    fast_cmd = (getattr(project, "fast_test_command", None) or "").strip()
    raw_target = (target or "").strip()

    if raw_target:
        if raw_target.startswith("-"):
            raise ValueError(f"Invalid test target path: {raw_target}")
        root = Path(project.root_path).resolve()
        target_abs = resolve_safe_path(root, raw_target)
        if not target_abs.exists():
            raise FileNotFoundError(f"Test target not found: {raw_target}")
        rel_posix = target_abs.relative_to(root).as_posix()

        resolved = None
        if target_abs.is_file():
            resolved = _quick_check_command_for_target(
                rel_posix,
                target_abs.suffix.lower(),
                post_cmd,
                fast_cmd,
                fast=bool(fast),
            )
        if resolved is not None:
            cmd, note = resolved
            return {
                "command": cmd,
                "is_full_suite": False,
                "mode": "targeted",
                "target": rel_posix,
                "note": note,
            }

        if fast_cmd:
            return {
                "command": fast_cmd.replace("{target}", rel_posix),
                "is_full_suite": False,
                "mode": "fast",
                "target": rel_posix,
                "note": f"Quick check command for {rel_posix}",
            }
        if post_cmd:
            return {
                "command": post_cmd,
                "is_full_suite": False,
                "mode": "targeted",
                "target": rel_posix,
                "note": f"Fallback to Post-Patch command (savepoint stays active for targeted check of {rel_posix})",
            }
        raise ValueError("No Quick Check Command or Post-Patch Test Command configured for this target.")

    if fast:
        if fast_cmd:
            return {
                "command": fast_cmd.replace("{target}", ".").strip(),
                "is_full_suite": False,
                "mode": "fast",
                "target": None,
                "note": "Configured Quick Check Command (savepoint stays active until full run_tests passes)",
            }
        if post_cmd:
            return {
                "command": post_cmd,
                "is_full_suite": False,
                "mode": "fast",
                "target": None,
                "note": "No Quick Check Command configured; running Post-Patch command (savepoint stays active because fast=true)",
            }
        raise ValueError("No Quick Check Command or Post-Patch Test Command is configured for this project.")

    if not post_cmd:
        raise ValueError("No Post-Patch Test Command is configured for this project.")

    return {
        "command": post_cmd,
        "is_full_suite": True,
        "mode": "full",
        "target": None,
        "note": "Full test suite (auto-commits active savepoint if all tests pass)",
    }
