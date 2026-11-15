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

from config import GEMINI_KEY_PATH
from models import AttachmentItem, ProjectProfile, SessionInfo, TokenStats
from services import fs_tools, key_store, patch_tools
from services.token_utils import estimate_tokens


READ_ONLY_TOOLS = {"list_tree", "search_code", "read_lines", "read_file"}
PATCH_TOOLS = {"patch_literal", "patch_lines", "write_file"}
SCRIPT_TOOLS = {"propose_script"}

BASE_SYSTEM_PROMPT = """You are a senior coding partner connected to the user's local workspace via human-gated tools.

TOKEN & EDITING DISCIPLINE:
1. Explore with `list_tree` and `search_code` BEFORE requesting `read_lines` or `read_file`.
2. Prefer `read_lines(path, start_line, end_line)` over `read_file` whenever files are large (note: `read_file` returns raw content without line numbers to save tokens; `search_code` and `read_lines` include compact line numbers).
3. Prefer `patch_literal` over `patch_lines` or `write_file` unless creating a new file or replacing most of a file.
4. For multi-file structural changes or custom code probes/packers, use `propose_script` so the user can review, write to disk, and run it locally.
5. Every tool call is gated by the user—explain briefly what you need and why. Be concise and direct.
"""


def build_tool_declarations() -> types.Tool:
    """Declares workspace tools for Gemini function calling."""
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
            name="search_code",
            description="Search codebase by substring or regex and return matching lines with context.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "pattern": types.Schema(type=types.Type.STRING, description="Substring or regex pattern"),
                    "is_regex": types.Schema(type=types.Type.BOOLEAN, description="Treat pattern as regex"),
                    "path_glob": types.Schema(type=types.Type.STRING, description="Optional glob filter e.g. '*.py' or 'core/**/*.py'"),
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
            name="propose_script",
            description="Propose a Python probe, packer, or patcher script to write to disk and optionally run in the project venv.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative script path, e.g. 'developer/probe_tmp.py'"),
                    "content": types.Schema(type=types.Type.STRING, description="Executable Python script content"),
                    "summary": types.Schema(type=types.Type.STRING, description="What this script checks or modifies"),
                },
                required=["path", "content"],
            ),
        ),
    ]
    return types.Tool(function_declarations=decls)


def load_system_instruction(project: ProjectProfile) -> str:
    """Builds system instruction combining token discipline + optional project Directives .md."""
    parts = [BASE_SYSTEM_PROMPT]
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
        res = fs_tools.list_tree(
            root,
            subpath=str(args.get("subpath", ".")),
            max_depth=int(args.get("max_depth", 3)),
            extra_ignores=ignores,
        )
        return {
            "category": "read",
            "title": f"List Tree: {args.get('subpath', '.')}",
            "estimated_tokens": res["estimated_tokens"],
            "detail": f"Depth {args.get('max_depth', 3)} · {len(res['entries'])} entries",
            "prepared_result": res,
        }

    if tool_name == "search_code":
        res = fs_tools.search_code(
            root,
            pattern=str(args.get("pattern", "")),
            is_regex=bool(args.get("is_regex", False)),
            path_glob=args.get("path_glob"),
            context_lines=int(args.get("context_lines", 2)),
            extra_ignores=ignores,
        )
        return {
            "category": "read",
            "title": f"Search Code: {args.get('pattern', '')}",
            "estimated_tokens": res["estimated_tokens"],
            "detail": f"{res['shown_hits']}/{res['total_hits']} matches",
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

    if tool_name == "propose_script":
        preview = patch_tools.preview_full_write(
            root, str(args["path"]), str(args["content"])
        )
        return {
            "category": "script",
            "title": f"Propose Script: {args['path']}",
            "summary": str(args.get("summary", "Executable Python script")),
            "script_content": str(args["content"]),
            "diff": preview["diff"],
            "ast_valid": preview["ast_valid"],
            "estimated_tokens": estimate_tokens(str(args["content"])),
            "prepared_patch": preview,
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
    ) -> Dict[str, Any]:
        """Executes one Gemini model turn with project directives and tool declarations."""
        client = self._get_client()
        sys_instruction = load_system_instruction(project)

        thinking_val = session.thinking_level.upper()
        if thinking_val == "XHIGH" and session.model != "gemini-4-hydrogen":
            thinking_val = "HIGH"

        config_kwargs: Dict[str, Any] = {
            "system_instruction": sys_instruction,
            "tools": [build_tool_declarations()],
        }
        if session.model.startswith("gemini-4") or session.model.startswith("gemini-3"):
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
                if getattr(part, "text", None):
                    text_parts.append(part.text)
                fc = getattr(part, "function_call", None)
                if fc:
                    args_dict = dict(fc.args) if fc.args else {}
                    function_calls.append({
                        "name": fc.name,
                        "args": args_dict,
                    })

        if not text_parts and getattr(response, "text", None):
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
        prompt = (
            "Write a concise, accurate git commit message (1-line subject <= 72 chars, "
            "optional short bullet body only if multiple logical changes exist) for this diff. "
            "Return ONLY the commit message text, no markdown fences.\n\n"
            f"{diff_text}"
        )
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
