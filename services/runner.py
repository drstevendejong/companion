"""
services/runner.py - Project-scoped command execution using target venv Python when detected.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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


async def run_project_command(
    project_root: str | Path,
    command: str,
    configured_python: Optional[str] = None,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Executes a command inside project_root and captures stdout/stderr + token estimate."""
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Project root directory not found: {root}")

    python_exe = resolve_project_python(root, configured_python)
    argv = _prepare_argv(command, python_exe)

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    timed_out = False
    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        stdout_bytes, _ = await proc.communicate()

    elapsed_ms = int((time.monotonic() - started) * 1000)
    output = stdout_bytes.decode("utf-8", errors="replace")
    truncated = False
    if len(output) > MAX_COMMAND_OUTPUT_CHARS:
        output = output[-MAX_COMMAND_OUTPUT_CHARS:]
        truncated = True

    return {
        "command": command,
        "resolved_argv": argv,
        "exit_code": -1 if timed_out else (proc.returncode if proc.returncode is not None else 0),
        "timed_out": timed_out,
        "duration_ms": elapsed_ms,
        "output": output,
        "truncated": truncated,
        "estimated_tokens": estimate_tokens(output),
    }
