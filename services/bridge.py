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
from typing import Any, Dict, List, Optional, Tuple

from config import DEFAULT_IGNORE_PATTERNS
from models import AttachmentItem, ProjectProfile
from services import fs_tools
from services.token_utils import estimate_tokens

_FALLBACK_BRIDGE_TOOL_CONTRACT = """--- COMPANION BRIDGE TOOL CONTRACT ---
STRICT OUTPUT RULES:
1) Add a plain-English one-liner BEFORE every JSON command, preferably with an indication of progress (e.g., "Step 2/4: Patching IMAP client...").
2) Generate AT MOST ONE ```json command block per message (if batching multiple actions, put them inside ONE JSON array `[...]` in that single ```json block).
3) Generate all output ONCE ONLY—never repeat, duplicate, or echo prose or JSON blocks in the same reply.

To inspect files or propose edits for human-gated diff review and auto-backup, emit a single ```json block containing a tool action object (or a JSON array of objects). All inspect/patch tools work generically on any UTF-8 text/code file type (Python, JS/TS, HTML/CSS, SQL, Go, Rust, config, etc.):
- {"tool": "patch_literal", "path": "path/to/file", "old_text": "exact unique block", "new_text": "replacement", "summary": "1-line reason"} (returns nearest line-number + diff diagnostics if old_text mismatches)
- {"tool": "patch_lines", "path": "path/to/file", "start_line": 10, "end_line": 20, "new_text": "replacement", "summary": "1-line reason"}
- {"tool": "write_file", "path": "path/to/file", "content": "full file text", "summary": "1-line reason"}
- {"tool": "propose_script", "path": "packer.py", "content": "python helper script text", "summary": "e.g. Pack target files into packed_context.md"}
- {"tool": "run_tests", "summary": "1-line reason"} (human-gated run of configured test suite; do NOT call on broken intermediate steps—only at the end of a complete edit batch)
- {"tool": "search_code", "pattern": "needle", "ignore_case": true, "is_regex": false} (supports `is_regex: true` + optional `"subpath"`, `"path_glob"`, `"start_line"`, `"end_line"`)
- {"tool": "read_lines", "path": "path/to/file", "start_line": 1, "end_line": 120}
- {"tool": "read_file", "path": "path/to/file"}
- {"tool": "list_tree", "subpath": ".", "max_depth": 3}

Notes:
- When you need multiple files/fragments from disk, emit a `propose_script` (e.g. `packer.py`) that writes a clean `.md` bundle file so the user can run it locally in 1 click and paste the `.md` back.
- Git status, diff viewing, commit message drafting, Commit, and Commit & Push are triggered by the user in Companion's right-drawer Git tab (not via JSON tool calls)."""


def get_playground_tool_contract() -> str:
    """Returns the latest Playground Bridge tool contract from static/prompts/bridge_tool_contract.txt."""
    from services.gemini_client import load_prompt_file

    return load_prompt_file("bridge_tool_contract.txt", _FALLBACK_BRIDGE_TOOL_CONTRACT)


PLAYGROUND_TOOL_CONTRACT = get_playground_tool_contract()

KNOWN_TOOLS = {
    "list_tree",
    "search_code",
    "read_lines",
    "read_file",
    "patch_literal",
    "patch_lines",
    "write_file",
    "delete_file",
    "rename_file",
    "propose_script",
    "run_tests",
}

KNOWN_TOOL_JSON_KEYS = {
    "tool",
    "tool_name",
    "name",
    "action",
    "args",
    "path",
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
}

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_VALID_JSON_ESCAPES = frozenset(('"', "\\", "/", "b", "f", "n", "r", "t"))
_KEY_AFTER_COMMA_RE = re.compile(r'^"([A-Za-z_][A-Za-z0-9_]{0,40})"\s*:')
_KNOWN_KEY_RE_STR = "|".join(sorted(KNOWN_TOOL_JSON_KEYS))
_CONTINUING_OBJ_RE = re.compile(rf'"\s*(?:\}}|,\s*"(?:{_KNOWN_KEY_RE_STR})"\s*:)')
_LEADING_JSON_HEADER_RE = re.compile(r"^(?:jsonc?|application/json)\s*\n([\s\S]+)$", re.IGNORECASE)
_LEADING_PY_HEADER_RE = re.compile(r"^(?:python3?|py)\s*\n([\s\S]+)$", re.IGNORECASE)
_INLINE_TOOL_HINT_RE = re.compile(r'"(?:tool|tool_name|name|action|actions|tool_calls)"\s*:')


def build_prompt_bundle(
    project: ProjectProfile,
    prompt: str,
    attachments: List[AttachmentItem],
    include_directives: bool = True,
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

        sections.append(get_base_system_prompt().strip())
        sections.append(get_playground_tool_contract().strip())

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
            f"> Note: {image_count} UI screenshot(s) attached locally — use Copy CDP Image to paste image(s) into Playground."
        )

    if clean_prompt:
        if sections:
            sections.append(f"## Request\n{clean_prompt}")
        else:
            sections.append(clean_prompt)

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


def _infer_python_script_path(code: str, heading_hint: str, index: int) -> Tuple[str, bool]:
    """
    Infers target script path from explicit comment (# path: ..., # file: ...)
    or surrounding markdown hint. Returns (path, has_explicit_marker).
    """
    top_lines = code.splitlines()[:6]
    for ln in top_lines:
        m = re.match(r"^\s*#\s*(?:path|file|filename)\s*[:=]\s*([^\s#]+)", ln, re.IGNORECASE)
        if m:
            return m.group(1).strip().strip("`'\""), True
        m2 = re.match(r"^\s*#\s*([A-Za-z0-9_./\\-]+\.py)\b", ln)
        if m2:
            return m2.group(1).strip(), True

    hint_match = re.search(r"`([A-Za-z0-9_./\\-]+\.py)`", heading_hint)
    if hint_match:
        return hint_match.group(1).lstrip("/"), True

    if re.search(r"\bpacker\.py\b", code) or re.search(r"\bpacker\.py\b", heading_hint, re.IGNORECASE):
        return "packer.py", False

    suffix = "" if index == 0 else f"_{index + 1}"
    return f"bridge_script{suffix}.py", False


def _looks_like_python_script(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
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
                ast.For,
                ast.With,
                ast.Try,
                ast.Call,
            ),
        )
        for node in ast.walk(tree)
    )


def parse_playground_markdown(raw_markdown: str) -> Dict[str, Any]:
    """
    Parses Playground output ("Copy as markdown", `.md` inside code block, or bare `json` / JSON copies)
    into prose_text + normalized Companion tool action requests.
    Enforces a hard stop immediately after the first valid JSON command block so runaway model
    hallucinations (e.g. "Wait for output", fake "## Local Workspace Tool Results", or extra blocks)
    are truncated and ignored.
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

    # 2. Extract fenced blocks and HARD-STOP at the end of the FIRST valid JSON command block
    blocks = _extract_fenced_blocks(cleaned)
    python_candidates: List[Dict[str, Any]] = []

    for blk in blocks:
        lang = blk["lang"]
        content = blk["content"]
        if lang in ("json", "jsonc", ""):
            found = _extract_actions_from_json_text(content)
            if found:
                truncated_prose = _strip_hallucinated_continuation(
                    cleaned[: blk["end_char"]].rstrip()
                )
                return {
                    "prose_text": truncated_prose,
                    "actions": found,
                }
        if lang in ("python", "py"):
            inferred_path, explicit = _infer_python_script_path(
                content, blk.get("heading_hint", ""), len(python_candidates)
            )
            python_candidates.append(
                {
                    "tool": "propose_script",
                    "args": {
                        "path": inferred_path,
                        "content": content,
                        "summary": f"Python script parsed from Playground block ({inferred_path})",
                    },
                    "end_char": blk["end_char"],
                    "explicit": explicit,
                }
            )

    # 3. Fallback: inline unfenced JSON (stopping after first JSON block), else standalone python block
    cleaned_pre_hallucination = _strip_hallucinated_continuation(cleaned)
    inline_actions, inline_end = _extract_inline_json_actions_with_end(cleaned_pre_hallucination)
    if inline_actions and inline_end is not None:
        return {
            "prose_text": cleaned_pre_hallucination[:inline_end].rstrip(),
            "actions": inline_actions,
        }

    if python_candidates:
        first_py = python_candidates[0]
        return {
            "prose_text": _strip_hallucinated_continuation(cleaned[: first_py["end_char"]].rstrip()),
            "actions": [{"tool": c["tool"], "args": c["args"]} for c in python_candidates],
        }

    m_py = _LEADING_PY_HEADER_RE.match(cleaned_pre_hallucination)
    if m_py:
        py_body = m_py.group(1).strip()
        if _looks_like_python_script(py_body):
            inferred_path, _ = _infer_python_script_path(py_body, "", 0)
            return {
                "prose_text": cleaned_pre_hallucination,
                "actions": [
                    {
                        "tool": "propose_script",
                        "args": {
                            "path": inferred_path,
                            "content": py_body,
                            "summary": f"Python script parsed from Playground paste ({inferred_path})",
                        },
                    }
                ],
            }

    return {
        "prose_text": cleaned_pre_hallucination,
        "actions": [],
    }


def serialize_tool_response(tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Shapes compact tool responses returned to Gemini or formatted for Playground."""
    if tool_name == "list_tree":
        return {"subpath": result["subpath"], "tree": result["tree"]}
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
        elif resp.get("applied"):
            blocks.append(f"- **{name}**: Patch applied to `{resp.get('path')}` (backup: `{resp.get('backup_path')}`)")
        elif resp.get("deleted"):
            blocks.append(f"- **{name}**: Deleted `{resp.get('path')}` (backup: `{resp.get('backup_path')}`)")
        elif resp.get("renamed"):
            blocks.append(
                f"- **{name}**: Renamed `{resp.get('old_path')}` → `{resp.get('new_path')}` (backup: `{resp.get('backup_path')}`)"
            )
        elif resp.get("written"):
            if resp.get("executed"):
                line = f"- **{name}**: Wrote `{resp.get('path')}` and executed (exit code `{resp.get('exit_code')}`)."
                preview = (resp.get("output_preview") or "").strip()
                if (resp.get("exit_code", 0) != 0 or resp.get("has_traceback")) and preview:
                    line += f"\n```text\n{preview}\n```"
                blocks.append(line)
            else:
                blocks.append(f"- **{name}**: Wrote `{resp.get('path')}` to disk.")
        elif name == "run_tests":
            cmd = resp.get("command", "tests")
            code = resp.get("exit_code", "?")
            passed = bool(resp.get("passed")) and code == 0 and not resp.get("has_traceback")
            status_lbl = "PASSED" if passed else "FAILED"
            scope_note = ""
            if resp.get("is_full_suite") is False:
                scope_note = " (quick/targeted check — savepoint stays active)"
            line = f"- **run_tests** (`{cmd}`): **{status_lbl}** (exit code `{code}`){scope_note}."
            preview = (resp.get("output_preview") or "").strip()
            if preview:
                line += f"\n```text\n{preview}\n```"
            blocks.append(line)
        else:
            blocks.append(f"### `{name}`\n```json\n{json.dumps(resp, indent=2)}\n```")
    return "\n\n".join(blocks).strip()


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
