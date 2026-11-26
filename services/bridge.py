"""
services/bridge.py - Playground Bridge workflow helpers (0 Gemini API tokens).

Supports:
- One-shot or per-session Prompt Bundle generation (Directives + Tool Contract + prompt + attachments)
- Robust parsing of AI Studio Playground outputs ("Copy as markdown" full output OR fenced .md/.json/.python blocks)
- Formatting approved local tool results & detecting newly generated .md packer outputs
"""

from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import DEFAULT_IGNORE_PATTERNS
from models import AttachmentItem, ProjectProfile
from services import fs_tools
from services.token_utils import estimate_tokens

DEFAULT_BRIDGE_GUARDRAIL = (
    "> **[BRIDGE REMINDER]** Batch all related file patches/edits in ONE reply (switch `# path:` inside one ```patch block "
    "or emit consecutive ```patch / `write_file` blocks). Add a 1-line summary right before each block/action stating intent + target path "
    "(`Inspection script (path.py): ...`, `New file (path.ext): ...`, or `Patching (path.ext): ...`). "
    "Emit raw ```patch or ```python (never compress Python inside JSON). "
    "HARD STOP immediately after your final closing ``` (or end of code)—zero trailing prose or fake output."
)

_FALLBACK_BRIDGE_TOOL_CONTRACT = """--- COMPANION BRIDGE TOOL CONTRACT ---
STRICT OUTPUT RULES:
1) Add a plain-English one-liner BEFORE every command or code block stating progress AND intent + target path, e.g.:
   - `Step 2/4 — Inspection script (inspect_tmp.py): Check database schema fields`
   - `Step 2/4 — New file (utils/helper.py): Create helper module`
   - `Step 2/4 — Patching (core/client.py): Fix retry timeout`
2) Prefer RAW fenced ```patch blocks for edits and RAW fenced ```python (or target-language) blocks for scripts and new files—never compress Python indentation inside JSON strings! **Batch multi-file changes in ONE reply**: either switch `# path: next/file.ext` before each hunk inside one ```patch block, or emit consecutive ```patch / tagged `write_file` blocks in the same reply. (For JSON commands, emit AT MOST ONE ```json array `[...]`.)
3) Generate all output ONCE ONLY—never repeat, duplicate, or echo prose, code, or JSON blocks in the same reply.
4) HARD STOP: End your response IMMEDIATELY after your **final** closing ``` (or end of code). Never emit `Output:`, `Wait for output`, `## Local Workspace Tool Results`, or any trailing prose afterward (Companion collects all patch/code blocks in your reply and only truncates trailing text after your final block).

RECOMMENDED ZERO-ESCAPING CODE FORMATS (NO JSON STRING ESCAPING NEEDED):
A) Surgical Code Edits inside ```patch — pick whichever form is most token-efficient for the edit (batch ALL files you already have context for in ONE reply—switch `# path:` inside one ```patch block or emit consecutive ```patch blocks rather than patching 1 file per turn):
1. Tight literal hunk (`patch_literal`) — for small/localized edits, include ONLY 2–4 unchanged boundary lines around the changed lines (never dump a whole 100-line function to change 5 lines; use multiple hunks and/or `# path:` headers in one ```patch block for multi-spot / multi-file edits):
```patch
# path: path/to/file_a.py
# summary: 1-line reason for file_a
<<<<<<< SEARCH
exact existing boundary lines (indentation preserved verbatim)
=======
replacement lines (indentation preserved verbatim)
>>>>>>> REPLACE
# path: path/to/file_b.js
# summary: 1-line reason for file_b
<<<<<<< SEARCH
existing lines in file_b
=======
updated lines in file_b
>>>>>>> REPLACE
```
2. Ellipsis `...` span inside `SEARCH` — to replace/delete a large block without knowing line numbers, emit 2–3 unique start lines, `...` on its own line, and 2–3 unique end lines (deterministic literal matching—NO regex; both start and end anchors must match uniquely):
```patch
# path: path/to/file.py
# summary: Replace large block via unique start/end anchors
<<<<<<< SEARCH
def big_func(arg):
    first_unique_line()
    ...
    last_unique_line()
    return result
=======
def big_func(arg):
    return fast_path(arg)
>>>>>>> REPLACE
```
3. Anchored line-range replacement (`patch_lines`) — when you read line numbers via `read_lines`/`search_code` and want to rewrite/delete a large range without echoing old code:
```patch
# path: path/to/file.py
# lines: 140-295
# expect_start: def big_func(arg):
# expect_end: return result
# summary: Rewrite big_func cleanly
def big_func(arg):
    return fast_path(arg)
```

B) Temporary Inspection / Verification Scripts (`propose_inspection_script` — NEVER for code edits; auto-deletes cleanly on exit 0 without leaving a savepoint) — state `Inspection script (path.py): ...` in the one-liner and emit a raw ```python block:
```python
# tool: propose_inspection_script
# path: developer/inspect_tmp.py
# summary: What this temporary inspection script checks or prints
import sys
print("diagnostic output")
```

C) New Files / Full Rewrites (`write_file`) — state `New file (path/to/file.ext): ...` in the one-liner and emit a fenced block in the target language with top directive comments:
`# tool: write_file`, `# path: path/to/file.py`, `# summary: 1-line reason` (also supports `// ...` or `<!-- ... -->`).

JSON COMMAND TOOLS (ideal for searches, targeted reads, file ops, tests, and re-running failed scripts):
- {"tool": "scout_workspace", "goal": "Trace how X is wired end-to-end and return verbatim patch-ready snippets + path:line citations", "subpath": ".", "max_steps": 10} (**PREFER FIRST when Scout is ENABLED** for any bug hunt, wiring trace, or unfamiliar component — runs in an ephemeral `-lite` loop and returns verbatim snippets/citations so you avoid multi-turn inspection!)
- {"tool": "extract_headers", "pattern": ".", "include_symbols": true, "max_files": 60} (0-token structural index of file header comments & top-level classes/functions)
- {"tool": "search_code", "pattern": "needle", "ignore_case": true, "is_regex": false} (supports `is_regex: true` + optional `"subpath"`, `"path_glob"`, `"start_line"`, `"end_line"`)
- {"tool": "read_lines", "path": "path/to/file", "start_line": 1, "end_line": 120} (preferred for targeted slices once exact file/lines are located)
- {"tool": "read_file", "path": "path/to/file"} (single small file or full rewrite only — NEVER batch multiple large `read_file` calls to hunt for a bug; use `scout_workspace` when enabled, or `search_code` + `read_lines`)
- {"tool": "read_command_output", "exec_id": "cmd_...", "start_line": 1, "end_line": 120} (inspect raw stdout/stderr from a buffered command; optional `"pattern"` or `"tail_lines"`, omit `exec_id` for latest)
- {"tool": "list_tree", "subpath": ".", "max_depth": 3}
- {"tool": "patch_literal", "path": "path/to/file", "old_text": "exact unique block (or start lines\\n...\\nend lines)", "new_text": "replacement", "summary": "1-line reason"}
- {"tool": "patch_lines", "path": "path/to/file", "start_line": 10, "end_line": 20, "new_text": "replacement", "expect_start": "optional first-line check", "expect_end": "optional last-line check", "summary": "1-line reason"}
- {"tool": "write_file", "path": "path/to/file", "content": "full file text", "summary": "1-line reason"}
- {"tool": "delete_file", "path": "path/to/file", "summary": "1-line reason"} (human-gated delete; auto-backed up first)
- {"tool": "rename_file", "old_path": "old/path", "new_path": "new/path", "summary": "1-line reason"} (human-gated move/rename; auto-backed up first)
- {"tool": "propose_inspection_script", "path": "inspect_tmp.py", "content": "...", "summary": "..."} (alias: `propose_script`; temporary inspection script—auto-deletes on clean exit 0; stays on disk only on error for patching)
- {"tool": "run_script", "path": "script.py", "summary": "1-line reason"} (human-gated run of a permanent/kept .py script in project Python; optional `"args": "--flag"`)
- {"tool": "run_and_delete_script", "path": "inspect_tmp.py", "summary": "1-line reason"} (ONLY to re-run a failed temporary script already saved on disk and delete it once it passes; to create+run a new temporary script use `propose_inspection_script`)
- {"tool": "run_tests", "summary": "1-line reason"} (human-gated full suite — auto-commits savepoint on pass; optional `"fast": true` / `"target": "file"` runs quick check)
- {"tool": "update_header", "paths": ["path/to/file1", "path/to/file2"]} (Scout-only: drafts updated top-of-file header comments as human-gated diff patches)

Notes:
- Call `scout_workspace` / `update_header` ONLY when Companion announces `Local AI Scout is NOW ENABLED`.
- When you need runtime state probing or multi-file mechanical inspection, emit a `propose_inspection_script` (e.g. raw ```python block printing stdout directly; Companion captures stdout and auto-deletes passing temporary scripts without leaving a savepoint). If a temporary inspection script fails and stays on disk, just emit a ```patch targeting it—Companion auto-approves patches/re-runs for already-approved inspection scripts and immediately re-runs & auto-deletes on pass (without needing a follow-up `run_and_delete_script`).
- Git status, diff viewing, commit message drafting, Commit, and Commit & Push are triggered by the user in Companion's right-drawer Git tab (not via JSON tool calls)."""


def get_default_playground_tool_contract() -> str:
    """Returns the built-in default Playground Bridge tool contract from static/prompts/bridge_tool_contract.txt."""
    from services.gemini_client import load_prompt_file

    return load_prompt_file("bridge_tool_contract.txt", _FALLBACK_BRIDGE_TOOL_CONTRACT)


def get_playground_tool_contract(db: Any = None) -> str:
    """Returns the active Playground Bridge tool contract (DB override if configured, else default file)."""
    if db is not None and hasattr(db, "get_setting"):
        override = db.get_setting("prompt_bridge_tool_contract")
        if isinstance(override, str) and override.strip():
            return override.strip()
    return get_default_playground_tool_contract()


def get_bridge_guardrail(db: Any = None) -> str:
    """Returns the every-prompt Bridge guardrail reminder (DB override if set; empty string disables it)."""
    if db is not None and hasattr(db, "get_setting"):
        override = db.get_setting("prompt_bridge_guardrail")
        if override is not None:
            return str(override).strip()
    return DEFAULT_BRIDGE_GUARDRAIL


PLAYGROUND_TOOL_CONTRACT = get_default_playground_tool_contract()

KNOWN_TOOLS = {
    "list_tree",
    "extract_headers",
    "search_code",
    "read_lines",
    "read_file",
    "read_command_output",
    "scout_workspace",
    "update_header",
    "patch_literal",
    "patch_lines",
    "write_file",
    "delete_file",
    "rename_file",
    "propose_inspection_script",
    "propose_script",
    "run_script",
    "run_and_delete_script",
    "run_tests",
}

KNOWN_TOOL_JSON_KEYS = {
    "tool",
    "tool_name",
    "name",
    "action",
    "args",
    "path",
    "paths",
    "old_path",
    "new_path",
    "from_path",
    "to_path",
    "overwrite",
    "subpath",
    "max_depth",
    "pattern",
    "is_regex",
    "ignore_case",
    "path_glob",
    "context_lines",
    "max_matches",
    "start_line",
    "end_line",
    "expect_start",
    "expect_end",
    "old_text",
    "new_text",
    "replace_all",
    "content",
    "summary",
    "actions",
    "tool_calls",
    "replace",
    "encoding",
    "reason",
    "fast",
    "target",
    "delete_on_success",
    "focus",
    "goal",
    "prompt",
    "max_steps",
    "exec_id",
    "include_symbols",
    "max_files",
    "tail_lines",
}

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_VALID_JSON_ESCAPES = frozenset(('"', "\\", "/", "b", "f", "n", "r", "t"))
_KEY_AFTER_COMMA_RE = re.compile(r'^"([A-Za-z_][A-Za-z0-9_]{0,40})"\s*:')
_KNOWN_KEY_RE_STR = "|".join(sorted(KNOWN_TOOL_JSON_KEYS))
_CONTINUING_OBJ_RE = re.compile(rf'"\s*(?:\}}|,\s*"(?:{_KNOWN_KEY_RE_STR})"\s*:)')
_LEADING_JSON_HEADER_RE = re.compile(r"^(?:jsonc?|application/json)\s*\n([\s\S]+)$", re.IGNORECASE)
_LEADING_PY_HEADER_RE = re.compile(r"^(?:python3?|py)\s*\n([\s\S]+)$", re.IGNORECASE)
_INLINE_TOOL_HINT_RE = re.compile(r'"(?:tool|tool_name|name|action|actions|tool_calls)"\s*:')


def build_scout_state_notice(enabled: bool) -> str:
    """Builds an explicit ON/OFF state notice appended when Scout status changes."""
    if enabled:
        return (
            "> **[COMPANION NOTICE] Local AI Scout is NOW ENABLED.** "
            "Delegate codebase exploration, wiring traces, and bug hunts to `{\"tool\": \"scout_workspace\", \"goal\": \"...\", \"subpath\": \".\", \"max_steps\": 10}` "
            "(ask for verbatim code slices so you don't need follow-up `read_lines` turns), `{\"tool\": \"update_header\", \"paths\": [\"...\"]}` "
            "for human-gated comment header updates, or add `\"focus\": \"question\"` to `run_tests` when Scout is enabled."
        )
    return (
        "> **[COMPANION NOTICE] Local AI Scout is NOW OFF.** "
        "Do NOT call `scout_workspace` or `update_header`. Use `extract_headers` (0-token), "
        "batched `search_code`/`read_lines`, and `read_command_output` for command output."
    )


def build_prompt_bundle(
    project: ProjectProfile,
    prompt: str,
    attachments: List[AttachmentItem],
    include_directives: bool = True,
    scout_enabled: Optional[bool] = None,
    scout_state_notice: str = "",
    db: Any = None,
    every_prompt_guardrail: Optional[str] = None,
) -> Dict[str, Any]:
    """Builds a clipboard-ready Markdown prompt bundle for AI Studio Playground."""
    sections: List[str] = []
    directives_included = False

    if include_directives:
        directives_included = True
        header_lines = [
            f"# Session Context — Project: {project.name}",
            f"- Root: `{project.root_path}`",
        ]
        sections.append("\n".join(header_lines))

        from services.gemini_client import get_base_system_prompt

        sections.append(get_base_system_prompt(db).strip())
        sections.append(get_playground_tool_contract(db).strip())

        if project.directives_path:
            try:
                dpath = fs_tools.resolve_safe_path(project.root_path, project.directives_path)
                if dpath.is_file():
                    text = dpath.read_text(encoding="utf-8", errors="replace").strip()
                    sections.append(
                        f"--- PROJECT DIRECTIVES ({project.directives_path}) ---\n{text}"
                    )
            except Exception:
                pass

    if scout_state_notice:
        sections.append(scout_state_notice.strip())
    elif include_directives and scout_enabled is not None:
        sections.append(build_scout_state_notice(scout_enabled))

    image_count = 0
    for att in attachments:
        if att.kind == "image":
            image_count += 1
            continue
        if att.kind == "snippet" and att.content.lstrip().startswith("## Local Workspace Tool Results"):
            sections.append(att.content.strip())
        else:
            sections.append(f"### [{att.kind.upper()}: {att.label}]\n```text\n{att.content.strip()}\n```")

    clean_prompt = (prompt or "").strip()
    if image_count > 0:
        sections.append(
            f"> Note: {image_count} image(s) attached locally — use Copy Image to paste image(s) into Playground."
        )

    if clean_prompt:
        if sections:
            sections.append(f"## Request\n{clean_prompt}")
        else:
            sections.append(clean_prompt)

    if every_prompt_guardrail is not None:
        guardrail_text = str(every_prompt_guardrail).strip()
    elif db is not None:
        guardrail_text = get_bridge_guardrail(db).strip()
    else:
        guardrail_text = ""

    if guardrail_text:
        sections.append(guardrail_text)
        if scout_enabled is True and not include_directives and not scout_state_notice:
            sections.append(
                "> *(Local AI Scout is ON — offload codebase/wiring exploration to `scout_workspace` (ask for verbatim snippets) instead of iterative `read_lines`/`read_file` or static inspection scripts.)*"
            )

    bundle_text = "\n\n".join(s for s in sections if s).strip()
    return {
        "bundle_text": bundle_text,
        "estimated_tokens": estimate_tokens(bundle_text) + (image_count * 258),
        "directives_included": directives_included,
        "image_count": image_count,
    }


def _extract_fenced_blocks(text: str, base_offset: int = 0) -> List[Dict[str, Any]]:
    """
    Stack-aware Markdown fenced-block extractor.
    Handles nested fences such as ```` ```markdown ... ```` ```` containing inner ```json / ```python blocks,
    and records exact end_char offsets in the outer text for hard-stop truncation.
    """
    lines_keep = text.splitlines(keepends=True)
    lines = [ln.rstrip("\r\n") for ln in lines_keep]
    line_starts: List[int] = []
    line_ends: List[int] = []
    pos = 0
    for ln_raw in lines_keep:
        line_starts.append(pos)
        pos += len(ln_raw)
        line_ends.append(pos)

    blocks: List[Dict[str, Any]] = []
    i = 0
    n = len(lines)
    fence_re = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")

    while i < n:
        m = fence_re.match(lines[i])
        if not m:
            i += 1
            continue
        fence = m.group(1)
        fence_char = fence[0]
        fence_len = len(fence)
        info = m.group(2).strip().lower()
        start_line = i
        j = i + 1
        body_lines: List[str] = []
        closed = False
        while j < n:
            m_close = fence_re.match(lines[j])
            if (
                m_close
                and m_close.group(1)[0] == fence_char
                and len(m_close.group(1)) >= fence_len
                and not m_close.group(2).strip()
            ):
                closed = True
                break
            body_lines.append(lines[j])
            j += 1

        content = "\n".join(body_lines)
        lang = info.split()[0] if info else ""
        if lang in ("markdown", "md") and ("```" in content or "~~~" in content):
            body_start_char = line_ends[start_line]
            inner_raw = "".join(lines_keep[start_line + 1 : j])
            inner = _extract_fenced_blocks(inner_raw, base_offset=base_offset + body_start_char)
            blocks.extend(inner)
        else:
            end_line_idx = j if closed else n - 1
            blocks.append(
                {
                    "lang": lang,
                    "content": content,
                    "start_line": start_line,
                    "end_line": end_line_idx,
                    "start_char": base_offset + line_starts[start_line],
                    "end_char": base_offset + (line_ends[end_line_idx] if n > 0 else 0),
                    "heading_hint": "\n".join(lines[max(0, start_line - 3) : start_line]),
                }
            )
        i = j + 1 if closed else n
    return blocks


def _normalize_action_dict(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    tool = raw.get("tool") or raw.get("tool_name") or raw.get("name") or raw.get("action")
    if not isinstance(tool, str):
        return None
    tool = tool.strip()
    if tool not in KNOWN_TOOLS:
        return None

    if isinstance(raw.get("args"), dict):
        args = dict(raw["args"])
    else:
        args = {
            k: v
            for k, v in raw.items()
            if k not in ("tool", "tool_name", "name", "action")
        }
    return {"tool": tool, "args": args}


def _collect_actions_from_parsed_json(parsed: Any) -> List[Dict[str, Any]]:
    candidates: List[Any]
    if isinstance(parsed, list):
        candidates = parsed
    elif isinstance(parsed, dict):
        if isinstance(parsed.get("actions"), list):
            candidates = parsed["actions"]
        elif isinstance(parsed.get("tool_calls"), list):
            candidates = parsed["tool_calls"]
        else:
            candidates = [parsed]
    else:
        return []

    out: List[Dict[str, Any]] = []
    for item in candidates:
        norm = _normalize_action_dict(item)
        if norm is not None:
            out.append(norm)
    return out


def _strip_leading_json_header(text: str) -> str:
    stripped = text.strip()
    m = _LEADING_JSON_HEADER_RE.match(stripped)
    if m:
        candidate = m.group(1).lstrip()
        if candidate.startswith("{") or candidate.startswith("["):
            return candidate
    return stripped


def _skip_ascii_ws(s: str, pos: int) -> int:
    n = len(s)
    while pos < n and s[pos] in " \t\r\n":
        pos += 1
    return pos


def _repair_one_sloppy_json_value(s: str, start: int = 0) -> Tuple[str, int]:
    """
    Repairs sloppy Playground-copied JSON starting at s[start] ('{' or '['):
    - Escapes unescaped inner double quotes inside string values (e.g. HTML class="..." or JS strings)
    - Escapes raw literal newlines/tabs/control chars inside JSON strings
    - Fixes invalid backslash escapes (e.g. regex '\\s' -> '\\\\s') while preserving valid '\\n', '\\"', etc.
    - Strips trailing commas before '}' or ']'
    Returns (repaired_json_text, end_src_index_in_s).
    """
    n = len(s)
    if start >= n or s[start] not in "{[":
        return "", start

    out: List[str] = []
    stack: List[str] = []
    # Top-level tracking for each stack frame: for 'obj', tracks expecting 'key'|'colon'|'value'|'comma'
    obj_expect: List[str] = []
    obj_key: List[str] = []
    last_comma_out_idx: Optional[int] = None
    i = start

    while i < n:
        ch = s[i]
        if ch in " \t\r\n":
            out.append(ch)
            i += 1
            continue

        if ch == "{":
            if stack and stack[-1] == "obj" and obj_expect[-1] == "value":
                obj_expect[-1] = "comma"
            last_comma_out_idx = None
            stack.append("obj")
            obj_expect.append("key")
            obj_key.append("")
            out.append("{")
            i += 1
            continue

        if ch == "[":
            if stack and stack[-1] == "obj" and obj_expect[-1] == "value":
                obj_expect[-1] = "comma"
            last_comma_out_idx = None
            stack.append("arr")
            obj_expect.append("value")
            obj_key.append("")
            out.append("[")
            i += 1
            continue

        if ch == "}":
            if last_comma_out_idx is not None:
                out[last_comma_out_idx] = ""
                last_comma_out_idx = None
            if stack and stack[-1] == "obj":
                stack.pop()
                obj_expect.pop()
                obj_key.pop()
            out.append("}")
            i += 1
            if not stack:
                return "".join(out), i
            continue

        if ch == "]":
            if last_comma_out_idx is not None:
                out[last_comma_out_idx] = ""
                last_comma_out_idx = None
            if stack and stack[-1] == "arr":
                stack.pop()
                obj_expect.pop()
                obj_key.pop()
            out.append("]")
            i += 1
            if not stack:
                return "".join(out), i
            continue

        if ch == ":":
            last_comma_out_idx = None
            if stack and stack[-1] == "obj":
                obj_expect[-1] = "value"
            out.append(":")
            i += 1
            continue

        if ch == ",":
            if stack and stack[-1] == "obj":
                obj_expect[-1] = "key"
            out.append(",")
            last_comma_out_idx = len(out) - 1
            i += 1
            continue

        if ch == '"':
            last_comma_out_idx = None
            in_obj = bool(stack and stack[-1] == "obj")
            is_key = in_obj and obj_expect[-1] == "key"
            current_key = obj_key[-1] if in_obj else ""
            out.append('"')
            i += 1
            key_chars: List[str] = []
            inner_brace_balance = 0

            while i < n:
                sch = s[i]
                if sch == "\\":
                    if i + 1 < n:
                        nxt = s[i + 1]
                        if nxt in _VALID_JSON_ESCAPES:
                            out.append("\\" + nxt)
                            if is_key:
                                key_chars.append(nxt)
                            i += 2
                            continue
                        if nxt == "u" and i + 6 <= n and all(c in _HEX_DIGITS for c in s[i + 2 : i + 6]):
                            out.append(s[i : i + 6])
                            i += 6
                            continue
                    out.append("\\\\")
                    i += 1
                    continue

                if sch == "\r":
                    i += 1
                    continue
                if sch == "\n":
                    out.append("\\n")
                    i += 1
                    continue
                if sch == "\t":
                    out.append("\\t")
                    i += 1
                    continue
                if ord(sch) < 0x20:
                    out.append(f"\\u{ord(sch):04x}")
                    i += 1
                    continue

                if sch == "{":
                    inner_brace_balance += 1
                elif sch == "}" and inner_brace_balance > 0:
                    inner_brace_balance -= 1

                if sch == '"':
                    j = _skip_ascii_ws(s, i + 1)
                    should_close = False
                    if j >= n:
                        should_close = True
                    elif is_key:
                        should_close = s[j] == ":"
                    elif stack and stack[-1] == "arr":
                        should_close = s[j] in (",", "]")
                    elif in_obj:
                        after = s[j]
                        if after == ",":
                            k = _skip_ascii_ws(s, j + 1)
                            if k >= n or s[k] == "}":
                                should_close = True
                            elif s[k] == '"':
                                km = _KEY_AFTER_COMMA_RE.match(s[k : k + 80])
                                if km and km.group(1) in KNOWN_TOOL_JSON_KEYS:
                                    should_close = True
                        elif after == "}":
                            k = _skip_ascii_ws(s, j + 1)
                            if k >= n or s[k] in ("]", "}", ",", "{"):
                                should_close = True
                            elif len(stack) == 1:
                                if current_key not in ("old_text", "new_text", "content"):
                                    should_close = True
                                elif (
                                    current_key != "old_text"
                                    and inner_brace_balance == 0
                                    and "\n" in s[j + 1 : k]
                                    and not _CONTINUING_OBJ_RE.search(s[k : k + 600])
                                ):
                                    should_close = True

                    if should_close:
                        out.append('"')
                        if is_key:
                            obj_key[-1] = "".join(key_chars)
                            obj_expect[-1] = "colon"
                        elif in_obj:
                            obj_expect[-1] = "comma"
                        i += 1
                        break

                    out.append('\\"')
                    i += 1
                    continue

                out.append(sch)
                if is_key:
                    key_chars.append(sch)
                i += 1
            continue

        # Numbers, booleans, null, or unexpected bare tokens
        last_comma_out_idx = None
        if stack and stack[-1] == "obj" and obj_expect[-1] == "value":
            obj_expect[-1] = "comma"
        out.append(ch)
        i += 1

    return "".join(out), i


def _extract_sloppy_json_actions(stripped: str) -> List[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    out: List[Dict[str, Any]] = []
    pos = 0
    n = len(stripped)
    while pos < n:
        pos = _skip_ascii_ws(stripped, pos)
        if pos >= n:
            break
        # Handle optional repeated 'json' header lines between adjacent blocks
        if stripped[pos] not in "{[":
            hdr = _LEADING_JSON_HEADER_RE.match(stripped[pos:])
            if hdr:
                pos += hdr.start(1)
                continue
            return []
        repaired, consumed_end = _repair_one_sloppy_json_value(stripped, pos)
        if not repaired:
            return []
        try:
            obj, _ = decoder.raw_decode(repaired, 0)
        except json.JSONDecodeError:
            return []
        out.extend(_collect_actions_from_parsed_json(obj))
        pos = consumed_end
    return out


def _extract_actions_from_json_text(raw_text: str) -> List[Dict[str, Any]]:
    stripped = _strip_leading_json_header(raw_text)
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
        return _collect_actions_from_parsed_json(parsed)
    except json.JSONDecodeError:
        pass

    # Support multiple adjacent strict JSON objects/arrays inside a single block
    decoder = json.JSONDecoder()
    out: List[Dict[str, Any]] = []
    pos = 0
    n = len(stripped)
    strict_ok = True
    while pos < n:
        while pos < n and stripped[pos].isspace():
            pos += 1
        if pos >= n:
            break
        try:
            obj, next_pos = decoder.raw_decode(stripped, pos)
        except json.JSONDecodeError:
            strict_ok = False
            break
        out.extend(_collect_actions_from_parsed_json(obj))
        pos = next_pos
    if strict_ok and out:
        return out

    # Fallback: repair unescaped HTML/JS quotes, raw newlines, invalid backslashes, or trailing commas
    return _extract_sloppy_json_actions(stripped)


_HALLUCINATED_STOP_RE = re.compile(
    r"(?m)^\s*(?:#{1,4}\s*Local Workspace Tool Results\b|Wait for output\b.*$)",
    re.IGNORECASE,
)
_TRAILING_HALLUCINATION_RE = re.compile(
    r"^\s*(?:(?:#{1,4}\s*)?(?:Local Workspace Tool Results|Output|Result|Console Output|Terminal Output|Expected Output)\s*:?|Wait for output\b)",
    re.IGNORECASE,
)
_NEW_FILE_INTENT_RE = re.compile(
    r"\b(?:new\s+(?:code\s+)?file|create\s+(?:new\s+)?(?:python\s+)?file|write_file|full\s+rewrite|rewrite\s+(?:file|module)|save\s+(?:as\s+)?(?:new\s+)?(?:code\s+)?file|add\s+(?:new\s+)?(?:code\s+)?(?:file|module))\b",
    re.IGNORECASE,
)
_INSPECT_INTENT_RE = re.compile(
    r"\b(?:inspection\s+script|temp(?:orary)?\s+(?:script|probe|inspector|check)|diagnostic\s+script|verification\s+script|probe\s+script|one-shot\s+(?:script|probe|check)|propose_inspection_script|propose_script|run\s+and\s+delete|inspect(?:ion|ing)?\b|diagnos(?:e|ing)|audit(?:ing)?|verify(?:ing)?\s+(?:schema|installed|api|fields|env))\b",
    re.IGNORECASE,
)
_UNFENCED_PY_START_RE = re.compile(
    r"^\s*(?:#\s*(?:tool|path|file|filename|summary)\s*[:=]|from\s+[A-Za-z_.]+\s+import\b|import\s+[A-Za-z_.]+|(?:async\s+)?def\s+[A-Za-z_]|class\s+[A-Za-z_]|@[A-Za-z_]|if\s+__name__\s*==|with\s+\S|try\s*:|[A-Za-z_][A-Za-z0-9_]*\s*=[^=])"
)


def _strip_hallucinated_continuation(text: str) -> str:
    m = _HALLUCINATED_STOP_RE.search(text)
    if m:
        return text[: m.start()].rstrip()
    return text


def _extract_inline_json_actions_with_end(text: str) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Fallback extractor for the FIRST unfenced inline JSON tool call block (strict or sloppy) mixed with prose."""
    decoder = json.JSONDecoder()
    pos = 0
    n = len(text)
    while pos < n:
        ch = text[pos]
        if ch in "{[":
            try:
                obj, next_pos = decoder.raw_decode(text, pos)
                found = _collect_actions_from_parsed_json(obj)
                if found:
                    return found, next_pos
            except json.JSONDecodeError:
                window = text[pos : pos + 350]
                if _INLINE_TOOL_HINT_RE.search(window):
                    repaired, consumed_end = _repair_one_sloppy_json_value(text, pos)
                    if repaired:
                        try:
                            obj, _ = decoder.raw_decode(repaired, 0)
                            found = _collect_actions_from_parsed_json(obj)
                            if found:
                                return found, max(pos + 1, consumed_end)
                        except json.JSONDecodeError:
                            pass
        pos += 1
    return [], None


def _extract_inline_json_actions(text: str) -> List[Dict[str, Any]]:
    actions, _ = _extract_inline_json_actions_with_end(text)
    return actions


def _extract_one_liner_summary(heading_hint: str) -> str:
    """Extracts the plain-English one-liner immediately preceding a block or code slice."""
    if not heading_hint:
        return ""
    for raw_ln in reversed(heading_hint.splitlines()):
        ln = raw_ln.strip()
        if not ln:
            continue
        if re.fullmatch(r"[-=*_`~]{3,}", ln) or re.fullmatch(r"#{1,6}\s*(?:patch|code|script)\s*", ln, re.IGNORECASE):
            continue
        ln = re.sub(r"^(?:[#>*-]+|\d+[.)])\s*", "", ln).strip()
        if ln.lower() in ("python", "python3", "py", "patch", "diff", "json"):
            continue
        if ln:
            return ln[:220]
    return ""


def _infer_python_script_path(code: str, heading_hint: str, index: int) -> Tuple[str, bool]:
    """
    Infers target script path from explicit comment (# path: ..., # file: ...)
    or surrounding markdown/one-liner hint. Returns (path, has_explicit_marker).
    """
    top_lines = code.splitlines()[:6]
    for ln in top_lines:
        m = re.match(r"^\s*#\s*(?:path|file|filename)\s*[:=]\s*([^\s#]+)", ln, re.IGNORECASE)
        if m:
            return m.group(1).strip().strip("`'\"").lstrip("/"), True
        m2 = re.match(r"^\s*#\s*([A-Za-z0-9_./\\-]+\.py)\b", ln)
        if m2:
            return m2.group(1).strip().lstrip("/"), True

    if heading_hint:
        hint_match = re.search(r"`([A-Za-z0-9_./\\-]+\.py)`", heading_hint)
        if hint_match:
            return hint_match.group(1).lstrip("/"), True
        bare_match = re.search(
            r"(?:^|[\s(`\"'])/?([A-Za-z0-9_./\\-]+\.py)(?=[\s)`\"':,;!?]|$)",
            heading_hint,
        )
        if bare_match:
            return bare_match.group(1).lstrip("/"), True

    if re.search(r"\bpacker\.py\b", code) or re.search(r"\bpacker\.py\b", heading_hint, re.IGNORECASE):
        return "packer.py", False

    suffix = "" if index == 0 else f"_{index + 1}"
    return f"bridge_script{suffix}.py", False


def _looks_like_python_script(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    body = list(getattr(tree, "body", []) or [])
    if not body:
        return False
    return any(
        isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Assign,
                ast.AugAssign,
                ast.AnnAssign,
                ast.For,
                ast.AsyncFor,
                ast.With,
                ast.AsyncWith,
                ast.Try,
                ast.Call,
            ),
        )
        for node in ast.walk(tree)
    )


def _classify_python_block_intent(
    heading_hint: str,
    directives: Dict[str, Any],
    code: str,
    inferred_path: str,
    has_explicit_path: bool,
) -> Tuple[str, bool]:
    """
    Determines whether a Python block is an explicit inspection script, an explicit write_file,
    a legacy packer.py script, or ambiguous (prompt user at gate).
    Returns (tool_name, intent_ambiguous).
    """
    if directives.get("has_explicit_tool") and directives.get("tool"):
        return str(directives["tool"]), False
    if (
        inferred_path == "packer.py"
        or re.search(r"\bpacker\.py\b", heading_hint or "", re.IGNORECASE)
        or re.search(r"\bpacker\.py\b", code or "")
    ):
        return "propose_script", False

    hint_text = heading_hint or ""
    if _NEW_FILE_INTENT_RE.search(hint_text):
        if has_explicit_path:
            return "write_file", False
        return "propose_inspection_script", True

    if _INSPECT_INTENT_RE.search(hint_text):
        return "propose_inspection_script", False

    return "propose_inspection_script", True


_DIRECTIVE_LINE_RE = re.compile(
    r"^\s*(?:(?:#|//|--)\s*|(?:/\*|<!--)\s*)?"
    r"(tool|path|file|filename|lines|line|start_line|end_line|expect_start|expect_end|summary|delete_on_success|replace_all|args|focus)\s*[:=]\s*(.+?)"
    r"(?:\s*(?:\*/|-->))?\s*$",
    re.IGNORECASE,
)
_SEARCH_START_RE = re.compile(r"^<{7}\s*(?:SEARCH|ORIGINAL|OLD)?\s*$", re.IGNORECASE)
_LINE_REPLACE_START_RE = re.compile(r"^<{7}\s*(?:REPLACE(?:MENT)?|NEW)\s*$", re.IGNORECASE)
_SEARCH_MID_RE = re.compile(r"^={7}\s*$")
_SEARCH_END_RE = re.compile(r"^>{7}\s*(?:REPLACE|NEW|END)?\s*$", re.IGNORECASE)
_LINE_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:(?:-|\.{2,}|:)\s*(\d+))?\s*$")


def _parse_line_range_spec(val: str) -> Optional[Tuple[int, int]]:
    m = _LINE_RANGE_RE.match(val.strip().strip("`'\"").lstrip("L"))
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) is not None else start
    return start, end


def _strip_wrapping_backticks(val: str) -> str:
    s = val.strip()
    if len(s) >= 2 and s.startswith("`") and s.endswith("`"):
        return s[1:-1].strip()
    return s


def _parse_bool_directive(val: str) -> Optional[bool]:
    low = val.strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    return None


def _parse_code_block_directives(content: str, heading_hint: str) -> Dict[str, Any]:
    """
    Inspects top comment directives inside a fenced code block, e.g.:
      # tool: propose_inspection_script
      # path: inspect_model.py
      # summary: Inspect installed SDK models
    or // tool: write_file, /* tool: write_file */, <!-- tool: write_file -->.
    Returns parsed directive metadata and `stripped_content` (with only pure Companion directive lines removed).
    """
    lines = content.splitlines()
    directives: Dict[str, Any] = {
        "tool": "",
        "path": "",
        "summary": "",
        "delete_on_success": None,
        "replace_all": None,
        "args": None,
        "focus": None,
        "has_explicit_tool": False,
        "has_explicit_path": False,
        "stripped_content": content,
    }
    directive_line_indices: set[int] = set()
    scan_limit = min(len(lines), 10)
    idx = 0
    # Skip optional blank line or shebang line without stripping shebang
    while idx < scan_limit:
        raw_ln = lines[idx]
        stripped_ln = raw_ln.strip()
        if not stripped_ln or stripped_ln.startswith("#!"):
            idx += 1
            continue
        m = _DIRECTIVE_LINE_RE.match(raw_ln)
        if not m:
            break
        # Require comment syntax (not plain JSON/YAML/Python assignments) for code blocks
        lead = raw_ln.lstrip()
        if not (
            lead.startswith("#")
            or lead.startswith("//")
            or lead.startswith("/*")
            or lead.startswith("<!--")
            or lead.startswith("--")
        ):
            break
        key = m.group(1).strip().lower()
        val = m.group(2).strip().strip("`")
        if key == "tool":
            t_norm = val.strip("'\"").strip()
            if t_norm in KNOWN_TOOLS:
                directives["tool"] = t_norm
                directives["has_explicit_tool"] = True
                directive_line_indices.add(idx)
        elif key in ("path", "file", "filename"):
            p_val = val.strip("'\" ").lstrip("/")
            if p_val and ("." in p_val or "/" in p_val or "\\" in p_val):
                directives["path"] = p_val
                directives["has_explicit_path"] = True
                directive_line_indices.add(idx)
        elif key == "summary":
            directives["summary"] = val.strip("'\"")
            directive_line_indices.add(idx)
        elif key == "delete_on_success":
            b = _parse_bool_directive(val)
            if b is not None:
                directives["delete_on_success"] = b
                directive_line_indices.add(idx)
        elif key == "replace_all":
            b = _parse_bool_directive(val)
            if b is not None:
                directives["replace_all"] = b
                directive_line_indices.add(idx)
        elif key == "args":
            directives["args"] = val.strip("'\"")
            directive_line_indices.add(idx)
        elif key == "focus":
            directives["focus"] = val.strip("'\"")
            directive_line_indices.add(idx)
        idx += 1

    if directive_line_indices:
        kept_lines = [ln for i, ln in enumerate(lines) if i not in directive_line_indices]
        while kept_lines and not kept_lines[0].strip():
            kept_lines.pop(0)
        body = "\n".join(kept_lines)
        if content.endswith("\n") and body and not body.endswith("\n"):
            body += "\n"
        directives["stripped_content"] = body

    if not directives["path"] and heading_hint:
        hint_match = re.search(r"`([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_-]+)`", heading_hint)
        if hint_match:
            directives["path"] = hint_match.group(1).lstrip("/")

    return directives


def _parse_patch_search_replace_block(content: str, heading_hint: str) -> List[Dict[str, Any]]:
    """
    Parses a raw ```patch / ```diff / ```companion-patch block containing either:
    1) One or more SEARCH/REPLACE hunks (`<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE`,
       with exact lines or unique `start / ... / end` ellipsis anchors), OR
    2) Line-range replacement hunks (`# lines: 140-295` with optional `# expect_start:` /
       `# expect_end:`, either as raw replacement body or wrapped in `<<<<<<< REPLACEMENT` / `>>>>>>> END`)
    into normalized `patch_literal` / `patch_lines` actions (zero JSON escaping needed).
    """
    default_path = ""
    if heading_hint:
        hint_match = re.search(r"`([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_-]+)`", heading_hint)
        if hint_match:
            default_path = hint_match.group(1).lstrip("/")
        else:
            bare_match = re.search(
                r"(?:^|[\s(`\"'])/?([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_-]+)(?=[\s)`\"':,;!?]|$)",
                heading_hint,
            )
            if bare_match:
                default_path = bare_match.group(1).lstrip("/")

    hint_summary = _extract_one_liner_summary(heading_hint)
    current_path = default_path
    current_summary = ""
    current_replace_all = False
    current_start_line: Optional[int] = None
    current_end_line: Optional[int] = None
    current_expect_start = ""
    current_expect_end = ""
    actions: List[Dict[str, Any]] = []

    state = "meta"
    search_lines: List[str] = []
    replace_lines: List[str] = []

    def _emit_line_patch(new_lines: List[str]) -> None:
        target_path = current_path.strip()
        if not target_path or current_start_line is None:
            return
        s_line = current_start_line
        e_line = current_end_line if current_end_line is not None else s_line
        args: Dict[str, Any] = {
            "path": target_path,
            "start_line": s_line,
            "end_line": e_line,
            "new_text": "\n".join(new_lines),
            "summary": current_summary or hint_summary or f"Line patch ({target_path}:{s_line}-{e_line})",
        }
        if current_expect_start:
            args["expect_start"] = current_expect_start
        if current_expect_end:
            args["expect_end"] = current_expect_end
        actions.append({"tool": "patch_lines", "args": args})

    def _reset_hunk_meta() -> None:
        nonlocal current_summary, current_replace_all, current_start_line, current_end_line, current_expect_start, current_expect_end
        current_summary = ""
        current_replace_all = False
        current_start_line = None
        current_end_line = None
        current_expect_start = ""
        current_expect_end = ""

    for ln in content.splitlines():
        if state == "meta":
            if current_start_line is not None and (_LINE_REPLACE_START_RE.match(ln) or _SEARCH_MID_RE.match(ln)):
                state = "replace"
                replace_lines = []
                continue
            if _SEARCH_START_RE.match(ln):
                state = "search"
                search_lines = []
                replace_lines = []
                current_start_line = None
                current_end_line = None
                current_expect_start = ""
                current_expect_end = ""
                continue
            m = _DIRECTIVE_LINE_RE.match(ln)
            if m:
                key = m.group(1).strip().lower()
                raw_val = m.group(2)
                val = raw_val.strip().strip("`'\"")
                if key in ("path", "file", "filename") and val:
                    current_path = val.lstrip("/")
                    continue
                if key in ("lines", "line"):
                    parsed_rng = _parse_line_range_spec(raw_val)
                    if parsed_rng is not None:
                        current_start_line, current_end_line = parsed_rng
                        continue
                elif key == "start_line":
                    try:
                        current_start_line = int(val)
                        continue
                    except ValueError:
                        pass
                elif key == "end_line":
                    try:
                        current_end_line = int(val)
                        continue
                    except ValueError:
                        pass
                elif key == "expect_start":
                    current_expect_start = _strip_wrapping_backticks(raw_val)
                    continue
                elif key == "expect_end":
                    current_expect_end = _strip_wrapping_backticks(raw_val)
                    continue
                elif key == "summary":
                    current_summary = val
                    continue
                elif key == "replace_all":
                    b = _parse_bool_directive(val)
                    if b is not None:
                        current_replace_all = b
                    continue
            if current_start_line is None:
                m_bare_file = re.match(r"^\s*(?:#|//)?\s*([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_-]+)\s*$", ln)
                if m_bare_file:
                    current_path = m_bare_file.group(1).lstrip("/")
                    continue
            if not ln.strip():
                continue
            if current_path.strip() and current_start_line is not None:
                state = "line_body"
                replace_lines = [ln]
        elif state == "line_body":
            replace_lines.append(ln)
        elif state == "search":
            if _SEARCH_MID_RE.match(ln):
                state = "replace"
            else:
                search_lines.append(ln)
        elif state == "replace":
            if _SEARCH_END_RE.match(ln):
                target_path = current_path.strip()
                if target_path:
                    if current_start_line is not None:
                        _emit_line_patch(replace_lines)
                    else:
                        args: Dict[str, Any] = {
                            "path": target_path,
                            "old_text": "\n".join(search_lines),
                            "new_text": "\n".join(replace_lines),
                            "summary": current_summary or hint_summary or f"Surgical patch ({target_path})",
                        }
                        if current_replace_all:
                            args["replace_all"] = True
                        actions.append({"tool": "patch_literal", "args": args})
                state = "meta"
                search_lines = []
                replace_lines = []
                _reset_hunk_meta()
            else:
                replace_lines.append(ln)

    if state == "line_body" and current_path.strip() and current_start_line is not None:
        _emit_line_patch(replace_lines)
    elif (
        state == "meta"
        and not actions
        and current_path.strip()
        and current_start_line is not None
        and current_end_line is not None
    ):
        _emit_line_patch([])

    return actions


def _extract_unfenced_patch_with_end(text: str) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Fallback extractor for unfenced `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` (or line-range replacement) blocks + trailing hallucination cut."""
    has_markers = "<<<<<<<" in text and ">>>>>>>" in text and (
        "=======" in text or re.search(r"^\s*(?:#|//|--)?\s*(?:lines?|start_line)\s*[:=]", text, re.IGNORECASE | re.MULTILINE)
    )
    if not has_markers:
        return [], None
    lines_keep = text.splitlines(keepends=True)
    lines = [ln.rstrip("\r\n") for ln in lines_keep]
    line_ends: List[int] = []
    pos = 0
    for ln_raw in lines_keep:
        pos += len(ln_raw)
        line_ends.append(pos)

    first_marker_idx = None
    last_end_idx = None
    for idx, ln in enumerate(lines):
        if first_marker_idx is None and (
            _SEARCH_START_RE.match(ln) or _LINE_REPLACE_START_RE.match(ln) or _DIRECTIVE_LINE_RE.match(ln)
        ):
            first_marker_idx = idx
        if _SEARCH_END_RE.match(ln):
            last_end_idx = idx

    if last_end_idx is None:
        return [], None

    meta_start = 0
    if first_marker_idx is not None:
        meta_start = max(0, first_marker_idx - 3)
        heading_hint = "\n".join(lines[meta_start:first_marker_idx])
    else:
        heading_hint = ""

    actions = _parse_patch_search_replace_block("\n".join(lines[: last_end_idx + 1]), heading_hint)
    if not actions:
        return [], None
    return actions, line_ends[last_end_idx]


def _extract_unfenced_python_with_bounds(text: str) -> Optional[Dict[str, Any]]:
    """
    Extracts an unfenced Python script (with optional preceding 1-liner prose and/or `python` UI label)
    and strips trailing hallucinated prose/output lines beyond the valid AST Python prefix.
    """
    lines_keep = text.splitlines(keepends=True)
    lines = [ln.rstrip("\r\n") for ln in lines_keep]
    if not lines:
        return None
    line_ends: List[int] = []
    pos = 0
    for ln_raw in lines_keep:
        pos += len(ln_raw)
        line_ends.append(pos)

    n = len(lines)
    start_idx: Optional[int] = None
    heading_end_idx = 0

    for idx, ln in enumerate(lines):
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.lower() in ("python", "python3", "py"):
            j = idx + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n:
                start_idx = j
                heading_end_idx = idx
                break
        if _UNFENCED_PY_START_RE.match(ln):
            start_idx = idx
            heading_end_idx = idx
            break

    if start_idx is None or start_idx >= n:
        return None

    limit_idx = n
    for idx in range(start_idx, n):
        if idx > start_idx and _TRAILING_HALLUCINATION_RE.match(lines[idx]):
            limit_idx = idx
            break

    best_end_idx: Optional[int] = None
    best_code = ""
    for end_idx in range(limit_idx - 1, start_idx - 1, -1):
        if not lines[end_idx].strip():
            continue
        candidate = "\n".join(lines[start_idx : end_idx + 1])
        if not _looks_like_python_script(candidate):
            continue
        try:
            tree = ast.parse(candidate)
        except SyntaxError:
            continue
        body = list(getattr(tree, "body", []) or [])
        if not body:
            continue
        last_node = body[-1]
        # Never swallow a trailing bare identifier/constant word such as "Done" or "Output"
        if isinstance(last_node, ast.Expr) and isinstance(last_node.value, (ast.Name, ast.Constant)):
            continue
        # Require at least one clear code statement (not just prose tokens)
        has_real_stmt = any(
            not (isinstance(st, ast.Expr) and isinstance(st.value, (ast.Name, ast.Constant)))
            for st in body
        )
        if not has_real_stmt:
            continue
        best_end_idx = end_idx
        best_code = candidate
        break

    if best_end_idx is None or not best_code.strip():
        return None

    heading_hint = "\n".join(lines[max(0, heading_end_idx - 4) : heading_end_idx])
    directives = _parse_code_block_directives(best_code, heading_hint)
    inferred_path, explicit_in_code = _infer_python_script_path(
        directives["stripped_content"] if directives["has_explicit_path"] else best_code,
        heading_hint,
        0,
    )
    target_path = directives["path"] or inferred_path
    has_explicit_path = bool(directives["has_explicit_path"] or explicit_in_code)
    one_liner = _extract_one_liner_summary(heading_hint)

    tool_name, intent_ambiguous = _classify_python_block_intent(
        heading_hint,
        directives,
        best_code,
        target_path,
        has_explicit_path,
    )
    action_args: Dict[str, Any] = {
        "path": target_path,
        "content": directives["stripped_content"] if (directives["has_explicit_tool"] or directives["has_explicit_path"]) else best_code,
        "summary": directives["summary"] or one_liner or f"Python block parsed from Playground ({target_path})",
    }
    if tool_name in ("propose_inspection_script", "propose_script"):
        if intent_ambiguous:
            action_args["intent_ambiguous"] = True
            action_args["has_explicit_path"] = has_explicit_path
        if directives["delete_on_success"] is not None:
            action_args["delete_on_success"] = directives["delete_on_success"]
        if directives["focus"]:
            action_args["focus"] = directives["focus"]

    return {
        "action": {"tool": tool_name, "args": action_args},
        "end_char": line_ends[best_end_idx],
    }


def parse_playground_markdown(raw_markdown: str) -> Dict[str, Any]:
    """
    Parses Playground output ("Copy as markdown", `.md` inside code block, bare `json`/JSON,
    raw ```patch SEARCH/REPLACE blocks, tagged raw code/script blocks, standalone/unfenced Python blocks,
    or unfenced SEARCH/REPLACE patches) into prose_text + normalized Companion tool action requests.
    Enforces hard stop on runaway hallucinated continuations.
    """
    cleaned = (raw_markdown or "").strip()
    if not cleaned:
        return {"prose_text": "", "actions": []}

    # Unwrap outer single markdown block if user copied ```markdown ... ``` wrapper directly
    outer_md = re.match(r"^\s*(`{3,})(?:markdown|md)?\s*\n([\s\S]*?)\n\1\s*$", cleaned, re.IGNORECASE)
    if outer_md:
        cleaned = outer_md.group(2).strip()

    # 1. Whole-payload raw JSON fallback (with or without standalone 'json' UI header line)
    deheadered = _strip_leading_json_header(cleaned)
    if deheadered.startswith("{") or deheadered.startswith("["):
        trimmed_raw = _strip_hallucinated_continuation(deheadered)
        direct_actions = _extract_actions_from_json_text(trimmed_raw)
        if direct_actions:
            return {"prose_text": "", "actions": direct_actions}

    # 2. Extract fenced blocks (before any hallucinated continuation)
    cleaned_pre_hallucination = _strip_hallucinated_continuation(cleaned)
    blocks = _extract_fenced_blocks(cleaned_pre_hallucination)
    explicit_actions: List[Dict[str, Any]] = []
    last_explicit_end = 0
    python_candidates: List[Dict[str, Any]] = []

    for idx, blk in enumerate(blocks):
        lang = blk["lang"]
        content = blk["content"]
        heading_hint = blk.get("heading_hint", "")
        one_liner = _extract_one_liner_summary(heading_hint)

        # 2a. JSON command block (hard stop right after it, or after its immediately paired code block)
        if lang in ("json", "jsonc", ""):
            found = _extract_actions_from_json_text(content)
            if found:
                stop_char = blk["end_char"]
                content_needing = [
                    a
                    for a in found
                    if a["tool"] in ("propose_inspection_script", "propose_script", "write_file", "run_and_delete_script")
                    and not str(a["args"].get("content") or "").strip()
                ]
                if len(content_needing) == 1 and (idx + 1) < len(blocks):
                    nxt_blk = blocks[idx + 1]
                    if nxt_blk["lang"] not in ("json", "jsonc", "patch", "companion-patch", "diff"):
                        nxt_dir = _parse_code_block_directives(nxt_blk["content"], nxt_blk.get("heading_hint", ""))
                        raw_body = nxt_dir["stripped_content"] if (nxt_dir["has_explicit_tool"] or nxt_dir["has_explicit_path"]) else nxt_blk["content"]
                        content_needing[0]["args"]["content"] = raw_body
                        if not content_needing[0]["args"].get("path") and nxt_dir.get("path"):
                            content_needing[0]["args"]["path"] = nxt_dir["path"]
                        if not content_needing[0]["args"].get("summary") and nxt_dir.get("summary"):
                            content_needing[0]["args"]["summary"] = nxt_dir["summary"]
                        stop_char = nxt_blk["end_char"]
                combined_actions = [*explicit_actions, *found] if explicit_actions else found
                return {
                    "prose_text": cleaned_pre_hallucination[:stop_char].rstrip(),
                    "actions": combined_actions,
                }

        # 2b. Raw SEARCH/REPLACE patch block (```patch / ```diff / conflict markers)
        has_search_replace = ("<<<<<<<" in content and "=======" in content and ">>>>>>>" in content)
        if lang in ("patch", "companion-patch", "diff") or (has_search_replace and lang != "markdown"):
            patch_acts = _parse_patch_search_replace_block(content, heading_hint)
            if patch_acts:
                explicit_actions.extend(patch_acts)
                last_explicit_end = blk["end_char"]
                continue

        # 2c. Tagged raw script or write_file block (any language with # tool: ... or # path: ...)
        directives = _parse_code_block_directives(content, heading_hint)
        dir_tool = directives["tool"]
        if dir_tool in ("propose_inspection_script", "propose_script", "write_file", "run_and_delete_script"):
            target_path = directives["path"]
            if not target_path and lang in ("python", "py"):
                target_path, _ = _infer_python_script_path(content, heading_hint, len(explicit_actions))
            if target_path:
                action_args: Dict[str, Any] = {
                    "path": target_path,
                    "content": directives["stripped_content"],
                    "summary": directives["summary"] or one_liner or f"{dir_tool} ({target_path})",
                }
                if directives["delete_on_success"] is not None:
                    action_args["delete_on_success"] = directives["delete_on_success"]
                if directives["args"]:
                    action_args["args"] = directives["args"]
                if directives["focus"]:
                    action_args["focus"] = directives["focus"]
                explicit_actions.append({"tool": dir_tool, "args": action_args})
                last_explicit_end = blk["end_char"]
                continue

        if lang in ("python", "py"):
            raw_body = directives["stripped_content"] if (directives["has_explicit_tool"] or directives["has_explicit_path"]) else content
            inferred_path, explicit_hint_path = _infer_python_script_path(
                raw_body, heading_hint, len(python_candidates) + len(explicit_actions)
            )
            target_path = directives["path"] or inferred_path
            has_explicit_path = bool(directives["has_explicit_path"] or explicit_hint_path)
            classified_tool, intent_ambiguous = _classify_python_block_intent(
                heading_hint,
                directives,
                raw_body,
                target_path,
                has_explicit_path,
            )
            action_args = {
                "path": target_path,
                "content": raw_body,
                "summary": directives["summary"] or one_liner or f"Python script parsed from Playground block ({target_path})",
            }
            if classified_tool in ("propose_inspection_script", "propose_script"):
                if directives["delete_on_success"] is not None:
                    action_args["delete_on_success"] = directives["delete_on_success"]
                if directives["focus"]:
                    action_args["focus"] = directives["focus"]
                if intent_ambiguous:
                    action_args["intent_ambiguous"] = True
                    action_args["has_explicit_path"] = has_explicit_path

            if not intent_ambiguous and classified_tool != "propose_script":
                explicit_actions.append({"tool": classified_tool, "args": action_args})
                last_explicit_end = blk["end_char"]
                continue

            python_candidates.append(
                {
                    "tool": classified_tool,
                    "args": action_args,
                    "end_char": blk["end_char"],
                    "explicit": has_explicit_path,
                }
            )

    if explicit_actions:
        return {
            "prose_text": cleaned_pre_hallucination[:last_explicit_end].rstrip(),
            "actions": explicit_actions,
        }

    # 3. Fallback: inline unfenced JSON (stopping after first JSON block), else fenced/unfenced Python or unfenced patch
    inline_actions, inline_end = _extract_inline_json_actions_with_end(cleaned_pre_hallucination)
    if inline_actions and inline_end is not None:
        return {
            "prose_text": cleaned_pre_hallucination[:inline_end].rstrip(),
            "actions": inline_actions,
        }

    if python_candidates:
        first_py = python_candidates[0]
        return {
            "prose_text": cleaned_pre_hallucination[: first_py["end_char"]].rstrip(),
            "actions": [{"tool": c["tool"], "args": c["args"]} for c in python_candidates],
        }

    unfenced_patch_actions, unfenced_patch_end = _extract_unfenced_patch_with_end(cleaned_pre_hallucination)
    if unfenced_patch_actions and unfenced_patch_end is not None:
        return {
            "prose_text": cleaned_pre_hallucination[:unfenced_patch_end].rstrip(),
            "actions": unfenced_patch_actions,
        }

    unfenced_py = _extract_unfenced_python_with_bounds(cleaned_pre_hallucination)
    if unfenced_py:
        return {
            "prose_text": cleaned_pre_hallucination[: unfenced_py["end_char"]].rstrip(),
            "actions": [unfenced_py["action"]],
        }

    return {
        "prose_text": cleaned_pre_hallucination,
        "actions": [],
    }


def serialize_tool_response(tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Shapes compact tool responses returned to Gemini or formatted for Playground."""
    if tool_name == "list_tree":
        return {"subpath": result["subpath"], "tree": result["tree"]}
    if tool_name == "extract_headers":
        return {
            "pattern": result.get("pattern", "."),
            "file_count": result.get("file_count", 0),
            "header_count": result.get("header_count", 0),
            "formatted": result.get("formatted", ""),
        }
    if tool_name == "search_code":
        return {
            "pattern": result["pattern"],
            "total_hits": result["total_hits"],
            "matches": result["formatted"],
        }
    if tool_name in ("read_lines", "read_file"):
        return {
            "path": result["path"],
            "total_lines": result["total_lines"],
            "content": result["content"],
        }
    if tool_name == "read_command_output":
        return {
            k: result.get(k)
            for k in (
                "found",
                "exec_id",
                "command",
                "exit_code",
                "passed",
                "total_lines",
                "shown_lines",
                "mode",
                "content",
                "error",
            )
            if result.get(k) is not None
        }
    if tool_name == "scout_workspace":
        return {
            k: result.get(k)
            for k in (
                "answer",
                "run_id",
                "steps_taken",
                "steps",
                "model_used",
                "models_hopped",
                "tokens_used",
                "stopped_early",
                "error",
            )
            if result.get(k) is not None
        }
    if tool_name == "update_header":
        return {
            k: result.get(k)
            for k in (
                "staged_count",
                "staged_paths",
                "unchanged_paths",
                "pending_gate_ids",
                "model",
                "tokens_used",
                "error",
            )
            if result.get(k) is not None
        }
    return result


def format_trailing_tool_events(messages: List[Any], *, errors_only: bool = False) -> str:
    """Formats contiguous trailing tool_event messages cleanly as Markdown for Playground."""
    trailing_events: List[Dict[str, Any]] = []
    for m in reversed(messages):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role != "tool_event":
            break
        raw_text = m.get("text", "") if isinstance(m, dict) else getattr(m, "text", "")
        try:
            ev = json.loads(raw_text)
        except Exception:
            continue
        if errors_only:
            resp = ev.get("response") or {}
            if not resp.get("error"):
                continue
        trailing_events.append(ev)
    trailing_events.reverse()
    return format_bridge_tool_results(trailing_events)


_FAILURE_LINE_RE = re.compile(
    r"(?:^\s*\[?\s*(?:ERROR|FAIL|FAILED|FATAL|CRITICAL|PANIC)\s*\]?(?:[:\s-]|$)"
    r"|^\s*(?:={12,}|--- FAIL:|E\s{2,}\S|[✖✗✘×❌]\s)"
    r"|\b(?:Traceback \(most recent call last\):|(?:Assertion|Syntax|Type|Reference|Value|Key|Attribute|Import|ModuleNotFound|Runtime|Name|Index|Indentation|ZeroDivision|OSError|FileNotFound|Permission)Error\b)"
    r"|(?::\d+(?::\d+)?|\(\d+(?:,\s*\d+)?\)):\s*(?:error|fatal)\b"
    r"|\berror(?:\[E\d+\]|\s+TS\d+):"
    r"|\b[1-9]\d*\s+(?:failed|errors?|failures?)\b)",
    re.IGNORECASE,
)


def format_command_output_preview(raw_output: str, max_chars: int = 12000) -> str:
    """Normalizes command stdout/stderr for clipboard/AI handoff, preserving head + tail if oversized."""
    text = (raw_output or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    head_len = int(max_chars * 0.45)
    tail_len = max_chars - head_len - 80
    omitted = len(text) - head_len - max(0, tail_len)
    return (
        f"{text[:head_len].rstrip()}\n\n"
        f"... [{omitted:,} middle chars omitted; full output is in Companion Console] ...\n\n"
        f"{text[-max(200, tail_len):].lstrip()}"
    )


def extract_failure_excerpt(raw_output: str, max_chars: int = 3500) -> str:
    """Extracts high-signal error/traceback windows (or tail fallback) from arbitrary command output."""
    text = (raw_output or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    lines = text.splitlines()
    ranges: List[Tuple[int, int]] = []
    n = len(lines)
    for idx, line in enumerate(lines):
        if _FAILURE_LINE_RE.search(line):
            start = max(0, idx - 1)
            end = min(n, idx + 11)
            if ranges and start <= ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
            else:
                ranges.append((start, end))

    if ranges:
        # Include tail summary too if not covered by last window
        tail_start = max(0, n - 8)
        if tail_start > ranges[-1][1] + 1:
            ranges.append((tail_start, n))
        elif tail_start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], n)

        chunks: List[str] = []
        total_len = 0
        for s_idx, e_idx in ranges:
            block = "\n".join(lines[s_idx:e_idx])
            sep = "\n...\n" if chunks else ""
            if total_len + len(sep) + len(block) > max_chars:
                remaining = max_chars - total_len - len(sep)
                if remaining > 120:
                    chunks.append(sep + block[:remaining] + "\n...[truncated]")
                break
            chunks.append(sep + block)
            total_len += len(sep) + len(block)
        out = "".join(chunks).strip()
        if out:
            return out

    tail = "\n".join(lines[-50:])
    if len(tail) > max_chars:
        tail = "...[truncated]\n" + tail[-max_chars:]
    return tail


def format_bridge_tool_results(tool_events: List[Dict[str, Any]]) -> str:
    """Formats executed local gate results and errors cleanly as Markdown to paste back to Playground."""
    if not tool_events:
        return ""
    blocks: List[str] = ["## Local Workspace Tool Results"]
    for ev in tool_events:
        name = ev.get("tool_name", "tool")
        resp = ev.get("response") or {}
        if resp.get("denied_by_user"):
            blocks.append(f"- **{name}**: Denied by user ({resp.get('reason', 'declined')})")
        elif resp.get("error"):
            target = resp.get("target")
            target_part = f" (`{target}`)" if target else ""
            blocks.append(f"- **{name}**{target_part} **Error**: {resp['error']}")
        elif name == "list_tree":
            blocks.append(f"### `list_tree` ({resp.get('subpath', '.')})\n```text\n{resp.get('tree', '')}\n```")
        elif name == "extract_headers":
            blocks.append(
                f"### `extract_headers` (`{resp.get('pattern', '.')}` · {resp.get('file_count', 0)} files, {resp.get('header_count', 0)} headers)\n"
                f"```text\n{resp.get('formatted', '')}\n```"
            )
        elif name == "search_code":
            blocks.append(
                f"### `search_code` (`{resp.get('pattern', '')}` · {resp.get('total_hits', 0)} hits)\n"
                f"```text\n{resp.get('matches', '')}\n```"
            )
        elif name in ("read_lines", "read_file"):
            p = resp.get("path", "")
            blocks.append(
                f"### `{name}` (`{p}` · {resp.get('total_lines', '?')} lines)\n"
                f"```text\n{resp.get('content', '')}\n```"
            )
        elif name == "read_command_output":
            eid = resp.get("exec_id") or "latest"
            if not resp.get("found"):
                blocks.append(f"- **read_command_output** (`{eid}`): Not found ({resp.get('error', 'buffer empty')})")
            else:
                blocks.append(
                    f"### `read_command_output` (`{eid}` · {resp.get('shown_lines', '?')}/{resp.get('total_lines', '?')} lines)\n"
                    f"```text\n{resp.get('content', '')}\n```"
                )
        elif name == "scout_workspace":
            steps_lbl = f"{resp.get('steps_taken', resp.get('steps_count', 0))} steps"
            model_lbl = resp.get("model_used") or resp.get("model") or "scout"
            stop_lbl = " · stopped early" if resp.get("stopped_early") else ""
            blocks.append(
                f"### `scout_workspace` ({model_lbl} · {steps_lbl}{stop_lbl})\n"
                f"{resp.get('answer', '').strip()}"
            )
        elif name == "update_header":
            staged = resp.get("staged_paths") or []
            unchanged = resp.get("unchanged_paths") or []
            blocks.append(
                f"- **update_header**: Drafted **{len(staged)}** human-gated header patch(es)"
                + (f" (`{', '.join(staged)}`)" if staged else "")
                + (f"; {len(unchanged)} unchanged" if unchanged else "")
                + "."
            )
        elif resp.get("applied"):
            auto_note = " *(auto-approved inspection script patch)*" if resp.get("auto_approved_inspection_script") else ""
            blocks.append(
                f"- **{name}**: Patch applied to `{resp.get('path')}` (backup: `{resp.get('backup_path')}`){auto_note}"
            )
        elif resp.get("renamed"):
            blocks.append(
                f"- **{name}**: Renamed `{resp.get('old_path')}` → `{resp.get('new_path')}` (backup: `{resp.get('backup_path')}`)"
            )
        elif resp.get("written"):
            if resp.get("executed"):
                del_note = " and deleted temporary script" if resp.get("deleted") else ""
                eid = resp.get("exec_id")
                eid_note = f" · `exec_id={eid}`" if eid else ""
                if resp.get("cancelled"):
                    status_tail = f"stopped by user (exit code `{resp.get('exit_code')}`{eid_note})"
                else:
                    status_tail = f"(exit code `{resp.get('exit_code')}`{eid_note})"
                line = f"- **{name}**: Wrote `{resp.get('path')}` and executed{del_note} {status_tail}."
                ai_sum = (resp.get("ai_summary") or "").strip()
                if ai_sum:
                    quoted = "\n> ".join(ai_sum.splitlines())
                    line += f"\n> **Scout Summary:** {quoted}"
                preview = (resp.get("output_preview") or "").strip()
                if preview:
                    line += f"\n```text\n{preview}\n```"
                elif ai_sum and eid:
                    line += f"\n*(Raw output omitted to save tokens — call `read_command_output` with `exec_id={eid}` if exact lines are needed.)*"
                blocks.append(line)
            else:
                blocks.append(f"- **{name}**: Wrote `{resp.get('path')}` to disk.")
        elif name in ("run_script", "run_and_delete_script"):
            cmd = resp.get("command") or resp.get("path") or "script"
            code = resp.get("exit_code", "?")
            cancelled = bool(resp.get("cancelled"))
            ok = (not cancelled) and (code == 0) and (not resp.get("has_traceback"))
            if cancelled:
                status_lbl = "STOPPED BY USER (kept on disk)" if name == "run_and_delete_script" else "STOPPED BY USER"
            elif name == "run_and_delete_script":
                status_lbl = (
                    "OK — script auto-deleted"
                    if ok and resp.get("deleted")
                    else ("OK" if ok else "FAILED (script kept on disk for patching)")
                )
            else:
                status_lbl = "OK" if ok else "FAILED"
            eid = resp.get("exec_id")
            eid_note = f" · `exec_id={eid}`" if eid else ""
            line = f"- **{name}** (`{cmd}`): **{status_lbl}** (exit code `{code}`{eid_note})."
            ai_sum = (resp.get("ai_summary") or "").strip()
            if ai_sum:
                quoted = "\n> ".join(ai_sum.splitlines())
                line += f"\n> **Scout Summary:** {quoted}"
            preview = (resp.get("output_preview") or "").strip()
            if preview:
                line += f"\n```text\n{preview}\n```"
            elif ai_sum and eid:
                line += f"\n*(Raw output omitted to save tokens — call `read_command_output` with `exec_id={eid}` if exact lines are needed.)*"
            blocks.append(line)
        elif resp.get("deleted"):
            blocks.append(f"- **{name}**: Deleted `{resp.get('path')}` (backup: `{resp.get('backup_path')}`)")
        elif name == "run_tests":
            cmd = resp.get("command", "tests")
            code = resp.get("exit_code", "?")
            cancelled = bool(resp.get("cancelled"))
            passed = (not cancelled) and bool(resp.get("passed")) and code == 0 and not resp.get("has_traceback")
            status_lbl = "STOPPED BY USER" if cancelled else ("PASSED" if passed else "FAILED")
            scope_note = ""
            if resp.get("is_full_suite") is False:
                scope_note = " (quick/targeted check — savepoint stays active)"
            eid = resp.get("exec_id")
            eid_note = f" · `exec_id={eid}`" if eid else ""
            line = f"- **run_tests** (`{cmd}`): **{status_lbl}** (exit code `{code}`{eid_note}){scope_note}."
            ai_sum = (resp.get("ai_summary") or "").strip()
            if ai_sum:
                quoted = "\n> ".join(ai_sum.splitlines())
                line += f"\n> **Scout Summary:** {quoted}"
            preview = (resp.get("output_preview") or "").strip()
            if preview:
                line += f"\n```text\n{preview}\n```"
            elif ai_sum and eid:
                line += f"\n*(Raw output omitted to save tokens — call `read_command_output` with `exec_id={eid}` if exact lines are needed.)*"
            blocks.append(line)
        else:
            blocks.append(f"### `{name}`\n```json\n{json.dumps(resp, indent=2)}\n```")
    return "\n\n".join(blocks).strip()


def normalize_project_rel_path(project_root: str | Path, raw_path: str) -> Optional[str]:
    """Resolves a relative path safely inside project_root into normalized POSIX form."""
    text = str(raw_path or "").strip()
    if not text:
        return None
    try:
        root = Path(project_root).resolve()
        target = fs_tools.resolve_safe_path(root, text)
        return target.relative_to(root).as_posix()
    except Exception:
        return None


def get_approved_inspection_scripts(
    messages: Sequence[Any],
    project_root: str | Path,
) -> Dict[str, Dict[str, Any]]:
    """
    Returns active inspection/temporary scripts already approved & executed in this session
    that are still present on disk (e.g., failed inspection scripts awaiting patch + rerun).
    Once an inspection script passes cleanly and is auto-deleted (or manually deleted),
    it is removed from this map so arbitrary future writes require normal approval.
    """
    root = Path(project_root).resolve()
    tracked: Dict[str, Dict[str, Any]] = {}

    for msg in messages or ():
        if getattr(msg, "role", None) != "tool_event":
            continue
        try:
            payload = json.loads(getattr(msg, "text", "") or "{}")
        except Exception:
            continue
        tname = payload.get("tool_name")
        resp = payload.get("response") or {}
        if not isinstance(resp, dict):
            continue

        raw_path = str(resp.get("path") or "").strip()
        rel_posix = normalize_project_rel_path(root, raw_path) if raw_path else None

        if tname == "delete_file" and resp.get("deleted") and rel_posix:
            tracked.pop(rel_posix, None)
            continue

        if tname == "rename_file" and resp.get("renamed"):
            old_rel = normalize_project_rel_path(root, str(resp.get("old_path") or ""))
            if old_rel:
                tracked.pop(old_rel, None)
            continue

        if tname in (
            "propose_inspection_script",
            "propose_script",
            "run_and_delete_script",
            "run_script",
        ) and resp.get("executed") and rel_posix:
            is_inspection = (
                tname in ("propose_inspection_script", "propose_script", "run_and_delete_script")
                or bool(resp.get("auto_delete_on_success"))
                or bool(resp.get("kept_for_patching"))
                or bool(resp.get("auto_approved_inspection_script"))
            )
            if not is_inspection:
                continue
            if resp.get("deleted"):
                tracked.pop(rel_posix, None)
                continue
            tracked[rel_posix] = {
                "path": rel_posix,
                "auto_delete": bool(
                    resp.get("auto_delete_on_success")
                    or tname in ("propose_inspection_script", "propose_script", "run_and_delete_script")
                    or resp.get("kept_for_patching")
                ),
                "args": resp.get("args"),
                "passed": bool(resp.get("passed")) and not bool(resp.get("cancelled")),
                "tool_name": tname,
            }

    active: Dict[str, Dict[str, Any]] = {}
    for rel_posix, info in tracked.items():
        try:
            if (root / rel_posix).is_file():
                active[rel_posix] = info
        except OSError:
            continue
    return active


def find_recent_markdown_outputs(
    project_root: str | Path,
    stdout_text: str = "",
    exclude_rel: Optional[str] = None,
    window_seconds: float = 45.0,
) -> List[Dict[str, Any]]:
    """
    Detects generated .md files (such as /packer.py output) mentioned in command stdout
    or recently modified inside the project root so Bridge mode can offer 1-click Copy.
    """
    root = Path(project_root).resolve()
    if not root.is_dir():
        return []

    found_paths: Dict[str, Path] = {}
    exclude_norm = (exclude_rel or "").replace("\\", "/").lstrip("./")

    # 1. Explicit .md filenames in stdout
    for match in re.finditer(r"([A-Za-z0-9_./\\-]+\.md)\b", stdout_text or "", re.IGNORECASE):
        raw_rel = match.group(1).replace("\\", "/").lstrip("./")
        if raw_rel == exclude_norm:
            continue
        try:
            cand = fs_tools.resolve_safe_path(root, raw_rel)
            if cand.is_file():
                found_paths[cand.relative_to(root).as_posix()] = cand
        except Exception:
            continue

    # 2. Recently modified .md files in top 2 directory levels
    now = time.time()
    ignored = set(DEFAULT_IGNORE_PATTERNS)

    def _scan(directory: Path, depth: int) -> None:
        if depth > 2:
            return
        try:
            items = list(directory.iterdir())
        except OSError:
            return
        for item in items:
            rel_posix = item.relative_to(root).as_posix()
            if item.name in ignored or (item.is_dir() and item.name.startswith(".")):
                continue
            if item.is_dir():
                _scan(item, depth + 1)
            elif item.is_file() and item.suffix.lower() == ".md":
                if rel_posix == exclude_norm:
                    continue
                try:
                    mtime = item.stat().st_mtime
                except OSError:
                    continue
                if now - mtime <= window_seconds:
                    found_paths[rel_posix] = item

    _scan(root, 0)

    out: List[Dict[str, Any]] = []
    for rel, path_obj in sorted(found_paths.items(), key=lambda kv: kv[1].stat().st_mtime, reverse=True):
        try:
            raw = path_obj.read_text(encoding="utf-8", errors="replace")
            out.append(
                {
                    "path": rel,
                    "size_bytes": path_obj.stat().st_size,
                    "estimated_tokens": estimate_tokens(raw),
                }
            )
        except OSError:
            continue
    return out[:6]


def pack_project_files(
    project: ProjectProfile,
    paths: Optional[List[str]] = None,
    filter_text: str = "",
    max_files: int = 25,
) -> Dict[str, Any]:
    """Packs selected or filtered project files into a compact Markdown bundle (0 API tokens)."""
    root = Path(project.root_path).resolve()
    rel_paths: List[str] = []

    if paths:
        rel_paths = [p.strip() for p in paths if p and p.strip()]
    else:
        tree_res = fs_tools.list_tree(
            root,
            subpath=".",
            max_depth=5,
            extra_ignores=project.ignore_patterns,
            max_entries=1000,
        )
        needle = (filter_text or "").strip().lower()
        for entry in tree_res["entries"]:
            if entry["is_dir"]:
                continue
            if needle and needle not in entry["path"].lower():
                continue
            if entry["size"] > 200_000:
                continue
            rel_paths.append(entry["path"])
            if len(rel_paths) >= max_files:
                break

    sections: List[str] = []
    packed_files: List[str] = []
    for rel in rel_paths[:max_files]:
        try:
            finfo = fs_tools.read_file(root, rel)
            ext = Path(rel).suffix.lstrip(".") or "text"
            sections.append(f"### `{rel}` ({finfo['total_lines']} lines)\n```{ext}\n{finfo['content']}\n```")
            packed_files.append(rel)
        except Exception:
            continue

    bundle = "\n\n".join(sections)
    return {
        "files": packed_files,
        "count": len(packed_files),
        "content": bundle,
        "estimated_tokens": estimate_tokens(bundle),
    }
