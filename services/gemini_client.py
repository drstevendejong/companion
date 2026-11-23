"""
services/gemini_client.py - Google GenAI wrapper with human-gated tool execution,
token-efficiency directives, usage tracking, and injectable client for zero-token tests.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from google import genai
from google.genai import types

from config import GEMINI_KEY_PATH, PROMPTS_DIR
from models import AttachmentItem, ProjectProfile, SessionInfo, TokenStats
from services import fs_tools, key_store, model_catalog, output_buffer, patch_tools
from services.token_utils import estimate_tokens


READ_ONLY_TOOLS = {
    "list_tree",
    "extract_headers",
    "search_code",
    "read_lines",
    "read_file",
    "read_command_output",
}
SCOUT_TOOLS = {"scout_workspace", "update_header"}
PATCH_TOOLS = {"patch_literal", "patch_lines", "write_file", "delete_file", "rename_file"}
SCRIPT_TOOLS = {"propose_script"}
COMMAND_TOOLS = {"run_tests", "run_script", "run_and_delete_script"}

_FALLBACK_SYSTEM_PROMPT = """You are a senior coding partner connected to the user's local workspace via human-gated tools.
Every tool round-trip re-sends conversation history and burns rate-limit tokens. Strictly minimize token usage and round-trips:

1. BRIEF RESPONSES & ANTI-REPETITION:
   - Be terse and direct. State findings, root causes, and next steps in a few crisp lines—never five-paragraph walkthroughs quoting code the user didn't ask to see.
   - Optionally end with "Need details before we proceed?" when deeper explanation could help.
   - Always add a single plain-English one-liner (preferably indicating progress, e.g. "Step 2/4: Reading IMAP sync handler...") right before every JSON/tool command.
   - Generate all output ONCE ONLY—never repeat, duplicate, or re-emit the same prose or tool/JSON block inside a reply.

2. DIRECTIVES SELF-CHECK (CRITICAL):
   - When Project Directives are loaded below, audit EVERY planned design, edit, and script against ALL Directives BEFORE proposing patches or scripts. Never emit code that violates Directives and forces a costly redo.

3. BATCHED & SINGLE-SHOT CODE INSPECTION:
   - Never dribble 1–2 small reads/searches across many turns. Batch all needed `search_code` / `read_lines` / `read_file` calls together in ONE turn.
   - For repo-wide audits, dependency graphs, impact analysis, or multi-file inspection, prefer ONE `propose_script` single-shot inspector/packer that prints a compact structured summary instead of 5–10 iterative tool hops.
   - Prefer `search_code` (supports `is_regex`, `path_glob`, `subpath`, and `start_line`/`end_line`) + targeted `read_lines(path, start_line, end_line)` over full `read_file` unless you truly need the whole file (`read_file` omits line numbers to save tokens).

4. SURGICAL PATCHING, SCRIPTS & ATOMIC TEST LOOPS:
   - Prefer `patch_literal` (minimal unique context) over `patch_lines` or `write_file`. Only use `write_file` for new files or >70% rewrites. If `patch_literal` fails to match, Companion returns line-number and nearest-diff diagnostics for fast self-correction.
   - Use `propose_script` for multi-file mechanical edits or single-shot inspectors (set `"delete_on_success": true` if temporary). If a script fails, PATCH the existing script with `patch_literal`/`patch_lines` and call `run_script` (to keep it) or `run_and_delete_script` (to run and automatically delete once it passes).
   - Companion automatically saves a reversible project checkpoint before the first write in a batch. Do NOT run tests on intermediate half-finished edits—only emit `run_tests` once a coherent batch of changes is complete and ready to verify.
   - Git operations (`git status`, `git diff`, commit drafting, Commit / Commit & Push) are handled directly by the user inside Companion's right-drawer Git tab, not via autonomous model tools.
"""

_FALLBACK_GIT_COMMIT_PROMPT = """Write a concise, accurate git commit message (1-line subject <= 72 chars, optional short bullet body only if multiple logical changes exist) for this diff. Return ONLY the commit message text, no markdown fences.

```diff
{diff}
```"""


def load_prompt_file(filename: str, fallback: str) -> str:
    """Reads an externalized prompt template from static/prompts/, falling back safely if absent."""
    try:
        path = Path(PROMPTS_DIR) / filename
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
    except OSError:
        pass
    return fallback.strip()


def get_base_system_prompt() -> str:
    """Returns the latest system prompt from static/prompts/system_prompt.txt."""
    return load_prompt_file("system_prompt.txt", _FALLBACK_SYSTEM_PROMPT)


def get_git_commit_prompt(diff_text: str) -> str:
    """Builds the git commit drafting prompt from static/prompts/git_commit_prompt.txt."""
    template = load_prompt_file("git_commit_prompt.txt", _FALLBACK_GIT_COMMIT_PROMPT)
    if "{diff}" in template:
        return template.replace("{diff}", diff_text)
    return f"{template}\n\n```diff\n{diff_text}\n```"


BASE_SYSTEM_PROMPT = get_base_system_prompt()


def build_tool_declarations(scout_enabled: bool = False) -> types.Tool:
    """Declares workspace tools for Gemini function calling."""
    focus_schema = (
        {
            "focus": types.Schema(
                type=types.Type.STRING,
                description="Optional question/focus for the Local AI Scout to extract from command stdout/stderr when summarizing large or diagnostic output.",
            )
        }
        if scout_enabled
        else {}
    )
    decls = [
        types.FunctionDeclaration(
            name="list_tree",
            description="List files and folders in the project directory (ignores .git, .venv, backup, etc.).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "subpath": types.Schema(type=types.Type.STRING, description="Relative directory path, default '.'"),
                    "max_depth": types.Schema(type=types.Type.INTEGER, description="Recursion depth (1-5), default 3"),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="extract_headers",
            description="Extract top-of-file header docstrings/comments and top-level symbols across files matching a path/glob (0 AI tokens; fast structural index).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "pattern": types.Schema(type=types.Type.STRING, description="Glob or directory/file pattern, e.g. 'src/**/*.py', '*.js', or '.'"),
                    "include_symbols": types.Schema(type=types.Type.BOOLEAN, description="Include top-level class/function names (default true)"),
                    "max_files": types.Schema(type=types.Type.INTEGER, description="Max files to inspect (default 60)"),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="search_code",
            description="Search text files and filenames across the codebase (any language) by literal or regex (`is_regex: true`), optionally scoped by subpath, path_glob, or start_line/end_line.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "pattern": types.Schema(type=types.Type.STRING, description="Substring or regex pattern"),
                    "is_regex": types.Schema(type=types.Type.BOOLEAN, description="Treat pattern as regex"),
                    "ignore_case": types.Schema(type=types.Type.BOOLEAN, description="Case-insensitive match (default true); set false for exact case"),
                    "subpath": types.Schema(type=types.Type.STRING, description="Optional relative folder or single file to scope the search"),
                    "path_glob": types.Schema(type=types.Type.STRING, description="Optional glob filter, e.g. '*.{py,js,html}' or 'src/**'. Omit to search all text files."),
                    "start_line": types.Schema(type=types.Type.INTEGER, description="Optional 1-based start line filter"),
                    "end_line": types.Schema(type=types.Type.INTEGER, description="Optional 1-based end line filter"),
                    "context_lines": types.Schema(type=types.Type.INTEGER, description="Context lines around each match, default 2"),
                },
                required=["pattern"],
            ),
        ),
        types.FunctionDeclaration(
            name="read_lines",
            description="Read a specific inclusive 1-based line range from a project file.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative path inside project"),
                    "start_line": types.Schema(type=types.Type.INTEGER, description="Start line (1-based)"),
                    "end_line": types.Schema(type=types.Type.INTEGER, description="End line (1-based)"),
                },
                required=["path", "start_line", "end_line"],
            ),
        ),
        types.FunctionDeclaration(
            name="read_file",
            description="Read an entire file as raw text (no line-number overhead). Use read_lines instead for targeted slices.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative path inside project"),
                },
                required=["path"],
            ),
        ),
        types.FunctionDeclaration(
            name="read_command_output",
            description="Read raw lines or grep-search inside a previously recorded command output buffer (`exec_id`) when a summarized test/script output needs full details.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "exec_id": types.Schema(type=types.Type.STRING, description="Optional command execution ID (defaults to most recent command output)"),
                    "start_line": types.Schema(type=types.Type.INTEGER, description="1-based start line"),
                    "end_line": types.Schema(type=types.Type.INTEGER, description="1-based end line"),
                    "pattern": types.Schema(type=types.Type.STRING, description="Optional case-insensitive filter pattern inside output"),
                    "tail_lines": types.Schema(type=types.Type.INTEGER, description="Optional number of trailing lines to return when start/end omitted"),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="patch_literal",
            description="Surgical exact-match string replacement in a file. Automatically backed up to project backup/.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative file path"),
                    "old_text": types.Schema(type=types.Type.STRING, description="Exact existing text block (must match uniquely unless replace_all=true)"),
                    "new_text": types.Schema(type=types.Type.STRING, description="Replacement text"),
                    "replace_all": types.Schema(type=types.Type.BOOLEAN, description="Replace all occurrences if true"),
                    "summary": types.Schema(type=types.Type.STRING, description="Short 1-line summary of change"),
                },
                required=["path", "old_text", "new_text"],
            ),
        ),
        types.FunctionDeclaration(
            name="patch_lines",
            description="Replace an inclusive 1-based line range in a file. Automatically backed up to project backup/.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative file path"),
                    "start_line": types.Schema(type=types.Type.INTEGER, description="First line to replace (1-based)"),
                    "end_line": types.Schema(type=types.Type.INTEGER, description="Last line to replace (1-based)"),
                    "new_text": types.Schema(type=types.Type.STRING, description="Replacement text"),
                    "summary": types.Schema(type=types.Type.STRING, description="Short 1-line summary of change"),
                },
                required=["path", "start_line", "end_line", "new_text"],
            ),
        ),
        types.FunctionDeclaration(
            name="write_file",
            description="Create a new file or overwrite an existing file cleanly (backed up first).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative file path"),
                    "content": types.Schema(type=types.Type.STRING, description="Full file contents"),
                    "summary": types.Schema(type=types.Type.STRING, description="Short 1-line summary of file write"),
                },
                required=["path", "content"],
            ),
        ),
        types.FunctionDeclaration(
            name="delete_file",
            description="Delete an obsolete project file after backing it up to backup/ (human-gated).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative file path to delete"),
                    "summary": types.Schema(type=types.Type.STRING, description="Short 1-line reason for deletion"),
                },
                required=["path"],
            ),
        ),
        types.FunctionDeclaration(
            name="rename_file",
            description="Rename or move an existing project file after backing it up to backup/ (human-gated).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "old_path": types.Schema(type=types.Type.STRING, description="Current relative file path"),
                    "new_path": types.Schema(type=types.Type.STRING, description="Destination relative file path"),
                    "overwrite": types.Schema(type=types.Type.BOOLEAN, description="Allow overwriting existing destination file (default false)"),
                    "summary": types.Schema(type=types.Type.STRING, description="Short 1-line reason for rename/move"),
                },
                required=["old_path", "new_path"],
            ),
        ),
        types.FunctionDeclaration(
            name="propose_script",
            description="Propose a Python probe, packer, or patcher script to write to disk and optionally run in the project venv (optionally auto-deleting on pass if delete_on_success=true).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative script path, e.g. 'developer/probe_tmp.py'"),
                    "content": types.Schema(type=types.Type.STRING, description="Executable Python script content"),
                    "delete_on_success": types.Schema(type=types.Type.BOOLEAN, description="Optional: auto-delete script once it exits cleanly (0 with no traceback); keeps on error for patching"),
                    "summary": types.Schema(type=types.Type.STRING, description="What this script checks or modifies"),
                    **focus_schema,
                },
                required=["path", "content"],
            ),
        ),
        types.FunctionDeclaration(
            name="run_script",
            description="Request human-gated execution of an existing (or earlier-in-batch) Python script inside the project venv.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative path to Python script inside project"),
                    "args": types.Schema(type=types.Type.STRING, description="Optional CLI arguments string to pass to the script"),
                    "summary": types.Schema(type=types.Type.STRING, description="Brief note on what running this script checks or performs"),
                    **focus_schema,
                },
                required=["path"],
            ),
        ),
        types.FunctionDeclaration(
            name="run_and_delete_script",
            description="Request human-gated execution of an inspection/temporary Python script in the project venv and automatically delete it once it succeeds cleanly (exit code 0 with no traceback). Keeps the script on failure so you can patch it.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative path to temporary/inspection Python script inside project"),
                    "args": types.Schema(type=types.Type.STRING, description="Optional CLI arguments string to pass to the script"),
                    "summary": types.Schema(type=types.Type.STRING, description="Brief note on what this inspection script checks before cleanup"),
                    **focus_schema,
                },
                required=["path"],
            ),
        ),
        types.FunctionDeclaration(
            name="run_tests",
            description="Request human-gated execution of the project's test suite (auto-commits savepoint on full pass). Optional fast=true or target runs a quick single-file/targeted check while keeping the savepoint active.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "fast": types.Schema(type=types.Type.BOOLEAN, description="Optional fast/quick-check mode (keeps active savepoint open until full run_tests passes)"),
                    "target": types.Schema(type=types.Type.STRING, description="Optional relative file path for quick syntax/targeted test check"),
                    "summary": types.Schema(type=types.Type.STRING, description="Brief note on what this check verifies"),
                    **focus_schema,
                },
            ),
        ),
    ]
    if scout_enabled:
        decls.extend(
            [
                types.FunctionDeclaration(
                    name="scout_workspace",
                    description="Delegate an iterative read-only codebase search/trace to the Local AI Scout (uses lightweight -lite model with stateless ephemeral history). Ideal for tracing call paths, finding references across files, or answering investigative questions in one turn.",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "goal": types.Schema(type=types.Type.STRING, description="Clear question or investigation goal for the Scout"),
                            "subpath": types.Schema(type=types.Type.STRING, description="Optional relative folder to prioritize (default '.')"),
                            "max_steps": types.Schema(type=types.Type.INTEGER, description="Optional max read iterations (1-8, default 8)"),
                        },
                        required=["goal"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="update_header",
                    description="Use Local AI Scout to draft updated top-of-file header comments/docstrings matching current implementation for the specified file(s), staged as human-gated diff patches.",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "paths": types.Schema(
                                type=types.Type.ARRAY,
                                items=types.Schema(type=types.Type.STRING),
                                description="List of relative file paths to inspect and draft header updates for",
                            ),
                        },
                        required=["paths"],
                    ),
                ),
            ]
        )
    return types.Tool(function_declarations=decls)


def load_system_instruction(project: ProjectProfile, scout_enabled: bool = False) -> str:
    """Builds system instruction combining externalized system_prompt.txt + optional project Directives .md."""
    parts = [get_base_system_prompt()]
    if scout_enabled:
        parts.append(
            "\n[LOCAL AI SCOUT: ENABLED] You have `scout_workspace` (delegated multi-step read-only investigation), `update_header` (human-gated header comment updates), and optional `\"focus\"` on `run_tests`/`run_script`/`run_and_delete_script`/`propose_script`. Always use `extract_headers` for fast 0-token structural overviews first."
        )
    else:
        parts.append(
            "\n[LOCAL AI SCOUT: OFF] Do NOT call `scout_workspace` or `update_header`. Use `extract_headers` (0-token) + batched `search_code`/`read_lines`, or `read_command_output` for command output."
        )
    if project.directives_path:
        try:
            dpath = fs_tools.resolve_safe_path(project.root_path, project.directives_path)
            if dpath.is_file():
                text = dpath.read_text(encoding="utf-8", errors="replace")
                parts.append(f"\n--- PROJECT DIRECTIVES ({project.directives_path}) ---\n{text}")
        except Exception:
            pass
    return "\n".join(parts)


def build_user_parts(prompt: str, attachments: List[AttachmentItem]) -> List[types.Part]:
    """Converts prompt + multimodal attachments into Gemini Content parts."""
    parts: List[types.Part] = []
    for att in attachments:
        if att.kind == "image" and att.mime_type:
            raw_b64 = att.content.split(",", 1)[-1]
            parts.append(
                types.Part.from_bytes(
                    data=base64.b64decode(raw_b64),
                    mime_type=att.mime_type,
                )
            )
        else:
            header = f"[{att.kind.upper()}: {att.label}]"
            parts.append(types.Part.from_text(text=f"{header}\n{att.content}"))
    if prompt.strip():
        parts.append(types.Part.from_text(text=prompt))
    elif not parts:
        parts.append(types.Part.from_text(text="(Empty prompt)"))
    return parts


def prepare_tool_gate_preview(
    project: ProjectProfile,
    tool_name: str,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Computes a pre-flight preview + estimated token cost (or unified diff + AST check)
    BEFORE executing a read or modifying any file.
    """
    root = project.root_path
    ignores = project.ignore_patterns

    if tool_name == "list_tree":
        sub = str(args.get("subpath") or args.get("path") or ".")
        depth = int(args.get("max_depth", 3))
        res = fs_tools.list_tree(
            root,
            subpath=sub,
            max_depth=depth,
            extra_ignores=ignores,
        )
        return {
            "category": "read",
            "title": f"List Tree: {sub}",
            "estimated_tokens": res["estimated_tokens"],
            "detail": f"Depth {depth} · {len(res['entries'])} entries",
            "prepared_result": res,
        }

    if tool_name == "extract_headers":
        pattern = str(args.get("pattern") or args.get("subpath") or ".")
        include_symbols = bool(args.get("include_symbols", True))
        max_files = int(args.get("max_files", 60) or 60)
        res = fs_tools.extract_headers(
            root,
            pattern=pattern,
            include_symbols=include_symbols,
            max_files=max_files,
            extra_ignores=ignores,
        )
        return {
            "category": "read",
            "title": f"Extract Headers: {pattern}",
            "estimated_tokens": res["estimated_tokens"],
            "detail": f"{res['file_count']} files · {res['header_count']} headers",
            "prepared_result": res,
        }

    if tool_name == "search_code":
        pattern = str(args.get("pattern", ""))
        path_glob = str(args.get("path_glob") or "").strip()
        subpath = str(args.get("subpath") or args.get("path") or "").strip()
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        is_regex = bool(args.get("is_regex", False))
        ignore_case = bool(args.get("ignore_case", True))
        raw_max = args.get("max_matches")
        max_matches = int(raw_max) if raw_max is not None and int(raw_max) > 0 else None
        res = fs_tools.search_code(
            root,
            pattern=pattern,
            is_regex=is_regex,
            path_glob=path_glob or None,
            context_lines=int(args.get("context_lines", 2)),
            max_matches=max_matches,
            extra_ignores=ignores,
            ignore_case=ignore_case,
            subpath=subpath or None,
            start_line=int(start_line) if start_line is not None else None,
            end_line=int(end_line) if end_line is not None else None,
        )
        scope_bits: List[str] = []
        if subpath and subpath != ".":
            scope_bits.append(subpath)
        if path_glob:
            scope_bits.append(path_glob)
        if start_line is not None or end_line is not None:
            scope_bits.append(f"L{start_line or 1}-{end_line or 'end'}")
        scope_label = f" in {' · '.join(scope_bits)}" if scope_bits else " across project"
        mode_label = "regex" if is_regex else "text"
        case_label = "ignore-case" if ignore_case else "exact-case"
        return {
            "category": "read",
            "title": f"Search Code: {pattern}{scope_label}",
            "estimated_tokens": res["estimated_tokens"],
            "detail": f"{res['shown_hits']}/{res['total_hits']} matches · {mode_label} · {case_label} · scope: {' · '.join(scope_bits) if scope_bits else '*'}",
            "prepared_result": res,
        }

    if tool_name == "read_lines":
        path = str(args["path"])
        s = int(args.get("start_line", 1))
        e = int(args.get("end_line", 200))
        res = fs_tools.read_lines(root, path, s, e)
        return {
            "category": "read",
            "title": f"Read Lines: {path} ({res['start_line']}–{res['end_line']} / {res['total_lines']})",
            "estimated_tokens": res["estimated_tokens"],
            "detail": f"Lines {res['start_line']} to {res['end_line']}",
            "path": path,
            "start_line": res["start_line"],
            "end_line": res["end_line"],
            "total_lines": res["total_lines"],
            "prepared_result": res,
        }

    if tool_name == "read_file":
        path = str(args["path"])
        res = fs_tools.read_file(root, path)
        kb = res["char_count"] / 1024
        return {
            "category": "read",
            "title": f"Read Full File: {path} ({res['total_lines']:,} lines · {kb:.1f} KB)",
            "estimated_tokens": res["estimated_tokens"],
            "detail": f"{res['total_lines']:,} lines · {res['char_count']:,} chars ({kb:.1f} KB)",
            "path": path,
            "start_line": 1,
            "end_line": res["total_lines"],
            "total_lines": res["total_lines"],
            "prepared_result": res,
        }

    if tool_name == "read_command_output":
        exec_id = str(args.get("exec_id") or "").strip() or None
        s_line = int(args["start_line"]) if args.get("start_line") is not None else None
        e_line = int(args["end_line"]) if args.get("end_line") is not None else None
        pat = str(args.get("pattern") or "").strip() or None
        t_lines = int(args["tail_lines"]) if args.get("tail_lines") is not None else None
        res = output_buffer.read_command_output(
            exec_id=exec_id,
            start_line=s_line,
            end_line=e_line,
            pattern=pat,
            tail_lines=t_lines,
        )
        return {
            "category": "read",
            "title": f"Read Command Output: {res.get('exec_id') or exec_id or 'latest'}",
            "estimated_tokens": int(res.get("estimated_tokens") or 0),
            "detail": (
                f"{res.get('total_lines', 0)} total lines · mode {res.get('mode', 'range')}"
                if res.get("found")
                else str(res.get("error") or "Output buffer empty")
            ),
            "prepared_result": res,
        }

    if tool_name == "scout_workspace":
        goal = str(args.get("goal") or args.get("prompt") or "").strip()
        if not goal:
            raise ValueError("scout_workspace requires 'goal'.")
        subpath = str(args.get("subpath") or ".").strip() or "."
        max_steps = max(1, min(8, int(args.get("max_steps") or 8)))
        return {
            "category": "scout",
            "title": f"AI Scout: {goal[:72]}",
            "summary": goal,
            "detail": f"Scope: {subpath} · Up to {max_steps} ephemeral read steps (stateless -lite model; never writes disk)",
            "goal": goal,
            "subpath": subpath,
            "max_steps": max_steps,
            "estimated_tokens": 600,
        }

    if tool_name == "update_header":
        raw_paths = args.get("paths") or args.get("path") or []
        if isinstance(raw_paths, str):
            paths_list = [p.strip() for p in raw_paths.split(",") if p.strip()]
        else:
            paths_list = [str(p).strip() for p in raw_paths if str(p).strip()]
        if not paths_list:
            raise ValueError("update_header requires non-empty 'paths'.")
        return {
            "category": "scout_headers",
            "title": f"Draft Header Updates ({len(paths_list)} file{'s' if len(paths_list) != 1 else ''})",
            "summary": f"Use Local AI Scout to inspect and draft header comment diffs for: {', '.join(paths_list[:4])}",
            "detail": f"Stages human-gated diff patches for: {', '.join(paths_list)}",
            "paths": paths_list,
            "estimated_tokens": 400 * max(1, len(paths_list)),
        }

    if tool_name == "patch_literal":
        preview = patch_tools.preview_literal_patch(
            root,
            str(args["path"]),
            str(args["old_text"]),
            str(args["new_text"]),
            bool(args.get("replace_all", False)),
        )
        return {
            "category": "patch",
            "title": f"Surgical Patch: {args['path']}",
            "summary": str(args.get("summary", "Literal replacement")),
            "diff": preview["diff"],
            "ast_valid": preview["ast_valid"],
            "estimated_tokens": preview["estimated_tokens"],
            "prepared_patch": preview,
        }

    if tool_name == "patch_lines":
        preview = patch_tools.preview_line_patch(
            root,
            str(args["path"]),
            int(args["start_line"]),
            int(args["end_line"]),
            str(args["new_text"]),
        )
        return {
            "category": "patch",
            "title": f"Line Patch: {args['path']} ({args['start_line']}–{args['end_line']})",
            "summary": str(args.get("summary", "Line-range replacement")),
            "diff": preview["diff"],
            "ast_valid": preview["ast_valid"],
            "estimated_tokens": preview["estimated_tokens"],
            "prepared_patch": preview,
        }

    if tool_name == "write_file":
        preview = patch_tools.preview_full_write(
            root, str(args["path"]), str(args["content"])
        )
        return {
            "category": "patch",
            "title": f"Write File: {args['path']}",
            "summary": str(args.get("summary", "Full file write")),
            "diff": preview["diff"],
            "ast_valid": preview["ast_valid"],
            "estimated_tokens": preview["estimated_tokens"],
            "prepared_patch": preview,
        }

    if tool_name == "delete_file":
        rel_path = str(args.get("path") or args.get("target") or "").strip()
        if not rel_path:
            raise ValueError("delete_file requires 'path'.")
        preview = patch_tools.preview_delete_file(root, rel_path)
        return {
            "category": "patch",
            "title": f"Delete File: {rel_path}",
            "summary": str(args.get("summary", "Remove obsolete file (backed up first)")),
            "diff": preview["diff"],
            "ast_valid": None,
            "estimated_tokens": preview["estimated_tokens"],
            "prepared_patch": preview,
        }

    if tool_name == "rename_file":
        old_path = str(args.get("old_path") or args.get("from_path") or args.get("path") or "").strip()
        new_path = str(args.get("new_path") or args.get("to_path") or "").strip()
        if not old_path or not new_path:
            raise ValueError("rename_file requires 'old_path' and 'new_path'.")
        preview = patch_tools.preview_rename_file(
            root,
            old_path,
            new_path,
            overwrite=bool(args.get("overwrite", False)),
        )
        return {
            "category": "patch",
            "title": f"Rename File: {preview['old_path']} → {preview['new_path']}",
            "summary": str(args.get("summary", "Rename/move file (backed up first)")),
            "diff": preview["diff"],
            "ast_valid": None,
            "estimated_tokens": preview["estimated_tokens"],
            "prepared_patch": preview,
        }

    if tool_name == "propose_script":
        preview = patch_tools.preview_full_write(
            root, str(args["path"]), str(args["content"])
        )
        return {
            "category": "script",
            "title": f"Propose Script: {args['path']}",
            "summary": str(args.get("summary", "Executable Python script")),
            "script_content": str(args["content"]),
            "delete_on_success": bool(args.get("delete_on_success", False)),
            "diff": preview["diff"],
            "ast_valid": preview["ast_valid"],
            "estimated_tokens": estimate_tokens(str(args["content"])),
            "prepared_patch": preview,
        }

    if tool_name in ("run_script", "run_and_delete_script"):
        import shlex as _shlex

        rel_path = str(args.get("path") or args.get("target") or "").strip()
        if not rel_path:
            raise ValueError(f"{tool_name} requires 'path'.")
        target_abs = fs_tools.resolve_safe_path(root, rel_path)
        if target_abs.suffix.lower() != ".py":
            raise ValueError(f"{tool_name} only supports .py scripts inside project (got '{rel_path}').")
        rel_posix = target_abs.relative_to(Path(root).resolve()).as_posix()
        raw_args = args.get("args")
        if isinstance(raw_args, list):
            arg_list = [str(x) for x in raw_args if str(x) != ""]
        elif isinstance(raw_args, str) and raw_args.strip():
            arg_list = _shlex.split(raw_args.strip(), posix=(os.name != "nt"))
        else:
            arg_list = []
        cmd = " ".join(["python", _shlex.quote(rel_posix), *(_shlex.quote(a) for a in arg_list)])
        exists_note = "" if target_abs.is_file() else " (will run after earlier batch file edits)"
        auto_delete = tool_name == "run_and_delete_script"
        delete_note = (
            f" · Deletes {rel_posix} automatically if exit code is 0 with no traceback (keeps on error for patching)."
            if auto_delete
            else ""
        )
        return {
            "category": "command",
            "title": (
                f"Run & Delete on Pass: {rel_posix}"
                if auto_delete
                else f"Run Script: {rel_posix}"
            ),
            "summary": str(
                args.get("summary")
                or (
                    f"Execute {rel_posix} and auto-delete if it succeeds"
                    if auto_delete
                    else f"Execute {rel_posix} in project Python"
                )
            ),
            "detail": f"Command: {cmd}{delete_note}{exists_note}",
            "command": cmd,
            "script_path": rel_posix,
            "auto_delete_on_success": auto_delete,
            "estimated_tokens": 200,
        }

    if tool_name == "run_tests":
        from services import runner as _runner

        fast = bool(args.get("fast", False))
        target = str(args.get("target") or "").strip() or None
        resolved = _runner.resolve_test_command(project, fast=fast, target=target)
        cmd = resolved["command"]
        is_full = bool(resolved["is_full_suite"])
        title_prefix = "Run Tests" if is_full else "Run Quick Check"
        default_summary = (
            "Verify full project test suite and commit active savepoint if all pass"
            if is_full
            else resolved["note"]
        )
        return {
            "category": "command",
            "title": f"{title_prefix}: {cmd}",
            "summary": str(args.get("summary") or default_summary),
            "detail": f"Command: {cmd} · {resolved['note']}",
            "command": cmd,
            "is_full_suite": is_full,
            "mode": resolved["mode"],
            "target": resolved["target"],
            "estimated_tokens": 180 if not is_full else 250,
        }

    raise ValueError(f"Unsupported tool: {tool_name}")


def _extract_usage(response: Any) -> TokenStats:
    meta = getattr(response, "usage_metadata", None)
    if not meta:
        return TokenStats()
    inp = int(getattr(meta, "prompt_token_count", 0) or 0)
    out = int(getattr(meta, "candidates_token_count", 0) or 0)
    think = int(getattr(meta, "thoughts_token_count", 0) or 0)
    total = int(getattr(meta, "total_token_count", 0) or (inp + out + think))
    return TokenStats(
        input_tokens=inp,
        output_tokens=out,
        thinking_tokens=think,
        total_tokens=total,
    )


class GeminiGateway:
    """Thin wrapper over google-genai supporting mock injection for tests."""

    def __init__(
        self,
        client_factory: Optional[Callable[[], Any]] = None,
        key_path: Path = GEMINI_KEY_PATH,
        key_validator: Optional[Callable[[str], tuple[bool, str]]] = None,
    ) -> None:
        self._client_factory = client_factory
        self._key_path = Path(key_path)
        self._key_validator = key_validator or key_store.validate_api_key_with_gemini
        self._cached_client: Any = None

    @property
    def key_path(self) -> Path:
        return self._key_path

    def reset_client(self) -> None:
        self._cached_client = None

    def verify_key(self, api_key: str) -> tuple[bool, str]:
        return self._key_validator(api_key)

    @property
    def catalog_cache_path(self) -> Path:
        return self._key_path.parent / "model_catalog_cache.json"

    def get_model_catalog(self, force_refresh: bool = False, probe: bool = True) -> Dict[str, Any]:
        """Lists live Gemini chat models filtered to free-usable models when on a Free Tier key."""
        api_key = ""
        if self._client_factory is not None:
            api_key = "injected-test-client"
            client = self._get_client()
        else:
            api_key, _source = key_store.load_api_key(self._key_path)
            if not api_key:
                return model_catalog.get_fallback_catalog(
                    notice="Configure a Gemini API key to load live models"
                )
            client = self._get_client()

        try:
            return model_catalog.fetch_dynamic_model_catalog(
                client=client,
                api_key=api_key,
                cache_path=self.catalog_cache_path,
                force_refresh=force_refresh,
                probe=probe,
            )
        except Exception:
            return model_catalog.get_fallback_catalog()

    def record_runtime_error(self, model_id: str, error_text: Any) -> Optional[str]:
        """Updates cached free/paid model knowledge if a live request hits a 429 or 404."""
        if self._client_factory is not None:
            api_key = "injected-test-client"
        else:
            api_key, _ = key_store.load_api_key(self._key_path)
        return model_catalog.record_model_runtime_error(
            self.catalog_cache_path, api_key, model_id, error_text
        )

    def _get_client(self) -> Any:
        if self._cached_client is not None:
            return self._cached_client
        if self._client_factory is not None:
            self._cached_client = self._client_factory()
            return self._cached_client
        api_key, _source = key_store.load_api_key(self._key_path)
        if not api_key:
            raise RuntimeError(
                "Gemini API key is not configured. Open the API Key settings modal to save your key."
            )
        self._cached_client = genai.Client(api_key=api_key)
        return self._cached_client

    def generate_turn(
        self,
        session: SessionInfo,
        project: ProjectProfile,
        history_contents: List[types.Content],
        scout_enabled: bool = False,
    ) -> Dict[str, Any]:
        """Executes one Gemini model turn with project directives and tool declarations."""
        client = self._get_client()
        sys_instruction = load_system_instruction(project, scout_enabled=scout_enabled)

        thinking_val = session.thinking_level.upper()
        if thinking_val == "XHIGH" and session.model != "gemini-4-hydrogen":
            thinking_val = "HIGH"

        config_kwargs: Dict[str, Any] = {
            "system_instruction": sys_instruction,
            "tools": [build_tool_declarations(scout_enabled=scout_enabled)],
        }
        if not session.model.startswith(("gemini-1.", "gemini-2.")):
            try:
                config_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_level=getattr(types.ThinkingLevel, thinking_val)
                )
            except AttributeError:
                pass

        response = client.models.generate_content(
            model=session.model,
            contents=history_contents,
            config=types.GenerateContentConfig(**config_kwargs),
        )

        text_parts: List[str] = []
        function_calls: List[Dict[str, Any]] = []
        candidates = getattr(response, "candidates", None) or []
        if candidates and getattr(candidates[0], "content", None):
            for part in candidates[0].content.parts or []:
                if getattr(part, "text", None) and not getattr(part, "thought", False):
                    text_parts.append(part.text)
                fc = getattr(part, "function_call", None)
                if fc:
                    args_dict = dict(fc.args) if fc.args else {}
                    tc_entry: Dict[str, Any] = {
                        "name": fc.name,
                        "args": args_dict,
                    }
                    fc_id = getattr(fc, "id", None)
                    if fc_id:
                        tc_entry["id"] = fc_id
                    thought_sig = getattr(part, "thought_signature", None)
                    if thought_sig:
                        if isinstance(thought_sig, (bytes, bytearray)):
                            tc_entry["thought_signature_b64"] = base64.b64encode(
                                bytes(thought_sig)
                            ).decode("ascii")
                        elif isinstance(thought_sig, str):
                            tc_entry["thought_signature_b64"] = base64.b64encode(
                                thought_sig.encode("utf-8")
                            ).decode("ascii")
                    function_calls.append(tc_entry)

        if not text_parts and not function_calls and not candidates and getattr(response, "text", None):
            text_parts.append(response.text)

        return {
            "text": "\n".join(text_parts).strip(),
            "function_calls": function_calls,
            "usage": _extract_usage(response),
        }

    def draft_commit_message(self, diff_text: str, model: str = "gemini-4-hydrogen") -> Dict[str, Any]:
        """Drafts a concise conventional commit message from a git diff."""
        if not diff_text.strip():
            return {"message": "chore: update workspace", "usage": TokenStats()}
        client = self._get_client()
        prompt = get_git_commit_prompt(diff_text)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        text = (getattr(response, "text", None) or "").strip()
        if text.startswith("```"):
            lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
            text = "\n".join(lines).strip()
        return {
            "message": text or "chore: apply updates",
            "usage": _extract_usage(response),
        }
