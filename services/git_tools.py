"""
services/git_tools.py - Git status, diff inspection, commit, and push inside target project.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List

from services.token_utils import estimate_tokens, truncate_head_tail


class GitOperationError(RuntimeError):
    """Raised when a git command fails."""


def _run_git(project_root: str | Path, args: List[str]) -> subprocess.CompletedProcess[str]:
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Project root not found: {root}")
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def get_git_status(project_root: str | Path) -> Dict[str, Any]:
    """Returns branch info, changed file list, and combined working/staged diff."""
    check = _run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
    if check.returncode != 0:
        return {
            "is_repo": False,
            "branch": None,
            "changed_files": [],
            "diff": "",
            "diff_tokens": 0,
            "error": check.stderr.strip() or "Not a git repository.",
        }

    status_res = _run_git(project_root, ["status", "--porcelain=v1", "-b"])
    lines = status_res.stdout.splitlines()
    branch = "HEAD"
    changed_files: List[Dict[str, str]] = []

    for line in lines:
        if line.startswith("## "):
            branch = line[3:].strip()
        elif len(line) >= 4:
            code = line[:2]
            path = line[3:].strip()
            changed_files.append({"status": code.strip() or "M", "path": path})

    unstaged = _run_git(project_root, ["diff"]).stdout
    staged = _run_git(project_root, ["diff", "--cached"]).stdout
    combined_diff = "\n".join(part for part in (staged, unstaged) if part.strip())
    clipped_diff, was_clipped = truncate_head_tail(combined_diff, max_chars=24_000)

    return {
        "is_repo": True,
        "branch": branch,
        "changed_files": changed_files,
        "diff": clipped_diff,
        "diff_truncated": was_clipped,
        "diff_tokens": estimate_tokens(clipped_diff),
    }


def commit_changes(
    project_root: str | Path,
    message: str,
    stage_all: bool = True,
    push: bool = False,
) -> Dict[str, Any]:
    """Stages (optionally all), commits with message, and optionally pushes."""
    clean_msg = message.strip()
    if not clean_msg:
        raise GitOperationError("Commit message cannot be empty.")

    if stage_all:
        add_res = _run_git(project_root, ["add", "-A"])
        if add_res.returncode != 0:
            raise GitOperationError(add_res.stderr.strip() or "git add failed.")

    commit_res = _run_git(project_root, ["commit", "-m", clean_msg])
    if commit_res.returncode != 0:
        detail = (commit_res.stderr or commit_res.stdout).strip()
        raise GitOperationError(detail or "git commit failed.")

    push_output = ""
    if push:
        push_res = _run_git(project_root, ["push"])
        if push_res.returncode != 0:
            detail = (push_res.stderr or push_res.stdout).strip()
            raise GitOperationError(f"Commit succeeded, but push failed: {detail}")
        push_output = (push_res.stdout or push_res.stderr).strip()

    return {
        "committed": True,
        "pushed": push,
        "commit_output": (commit_res.stdout or commit_res.stderr).strip(),
        "push_output": push_output,
    }
