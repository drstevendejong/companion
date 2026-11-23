"""
services/local_scout.py - Ephemeral, read-only local AI Scout using lightweight -lite Gemini models.

Provides:
- Fresh stateless inspection loops (`run_scout_workspace`) with Pause/Resume/Stop controls & live WebSocket step balloons
- Automatic model hopping across usable `-lite` models at MEDIUM thinking on 429/quota errors (without dipping into primary Flash/Pro)
- Large/focused command output summarization & test failure triage (`summarize_command_output`, `triage_command_failure`)
- Opt-in file header comment updater (`draft_header_updates`) and Scout-backed Git commit message drafting
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from google.genai import types

from config import (
    OUTPUT_SUMMARIZE_THRESHOLD_CHARS,
    SCOUT_COOLDOWN_SECONDS,
    SCOUT_DEFAULT_MAX_STEPS,
    SCOUT_DEFAULT_MAX_TOKENS,
    SCOUT_MAX_TOOL_READ_CHARS,
)
from models import ProjectProfile, TokenStats
from services import fs_tools, model_catalog, patch_tools, runner
from services.token_utils import estimate_tokens

_COOLDOWN_LOCK = threading.Lock()
_MODEL_COOLDOWNS: Dict[str, float] = {}

_RUNS_LOCK = threading.Lock()
_ACTIVE_RUNS: Dict[str, "ScoutRunController"] = {}


def clear_cooldowns() -> None:
    """Clears in-memory model cooldowns (mainly for deterministic tests)."""
    with _COOLDOWN_LOCK:
        _MODEL_COOLDOWNS.clear()


def _mark_cooldown(model_id: str, seconds: int = SCOUT_COOLDOWN_SECONDS) -> None:
    with _COOLDOWN_LOCK:
        _MODEL_COOLDOWNS[model_id] = time.time() + max(5, int(seconds))


def _snapshot_cooldowns() -> Dict[str, float]:
    now = time.time()
    with _COOLDOWN_LOCK:
        expired = [k for k, v in _MODEL_COOLDOWNS.items() if v <= now]
        for k in expired:
            del _MODEL_COOLDOWNS[k]
        return dict(_MODEL_COOLDOWNS)


class ScoutRunController:
    """Thread-safe Pause/Resume/Stop controller for an in-flight Scout run."""

    def __init__(self, run_id: str, session_id: str, goal: str, max_steps: int) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.goal = goal
        self.max_steps = max_steps
        self._resume_event = threading.Event()
        self._resume_event.set()
        self.paused = False
        self.stop_requested = False

    def pause(self) -> None:
        self.paused = True
        self._resume_event.clear()

    def resume(self) -> None:
        self.paused = False
        self._resume_event.set()

    def stop(self) -> None:
        self.stop_requested = True
        self.paused = False
        self._resume_event.set()

    def wait_if_paused(self, on_pause_state: Optional[Callable[[bool], None]] = None) -> bool:
        """Blocks cooperatively while paused; returns False if stop_requested becomes True."""
        if self.stop_requested:
            return False
        if not self._resume_event.is_set():
            if on_pause_state:
                on_pause_state(True)
            while not self._resume_event.wait(timeout=0.15):
                if self.stop_requested:
                    return False
            if on_pause_state:
                on_pause_state(False)
        return not self.stop_requested


def register_scout_run(ctrl: ScoutRunController) -> None:
    with _RUNS_LOCK:
        _ACTIVE_RUNS[ctrl.run_id] = ctrl


def unregister_scout_run(run_id: str) -> None:
    with _RUNS_LOCK:
        _ACTIVE_RUNS.pop(run_id, None)


def get_scout_run(run_id: str) -> Optional[ScoutRunController]:
    with _RUNS_LOCK:
        return _ACTIVE_RUNS.get(run_id)


def _emit_ws(app: Any, loop: Optional[asyncio.AbstractEventLoop], payload: Dict[str, Any]) -> None:
    if app is None:
        return
    coro = runner.broadcast_exec_event(app, payload)
    if loop is not None and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
            return
        except Exception:
            pass
    try:
        asyncio.run(coro)
    except Exception:
        pass


def _make_text_content(role: str, text: str) -> Any:
    if hasattr(types, "Content") and hasattr(types, "Part") and hasattr(types.Part, "from_text"):
        try:
            return types.Content(role=role, parts=[types.Part.from_text(text=text)])
        except Exception:
            pass
    return {"role": role, "parts": [{"text": text}]}


def _resolve_thinking_config(model_id: str, desired_level: str) -> Any:
    """Builds the lowest-overhead ThinkingConfig supported by `model_id` for `desired_level`."""
    mid = (model_id or "").lower()
    if mid.startswith(("gemini-1.", "gemini-2.0")):
        return None
    if not hasattr(types, "ThinkingConfig"):
        return None
    lvl_upper = (desired_level or "LOW").strip().upper()
    if lvl_upper in ("NONE", "OFF"):
        return None

    # Gemini 2.5 models use numeric thinking_budget (0 disables thinking cleanly)
    if mid.startswith("gemini-2.5"):
        budget = 0 if lvl_upper in ("MINIMAL", "NONE") else 1024
        try:
            return types.ThinkingConfig(thinking_budget=budget)
        except Exception:
            return None

    if hasattr(types, "ThinkingLevel"):
        lvl_obj = getattr(types.ThinkingLevel, lvl_upper, None)
        if lvl_obj is None and lvl_upper == "MINIMAL":
            lvl_obj = getattr(types.ThinkingLevel, "LOW", None)
        if lvl_obj is not None:
            try:
                return types.ThinkingConfig(thinking_level=lvl_obj)
            except Exception:
                return None
    return None


def _build_scout_config(
    system_instruction: str,
    model_id: str,
    tools: Optional[List[Any]] = None,
    thinking_level: str = "LOW",
) -> Any:
    cfg: Dict[str, Any] = {"system_instruction": system_instruction}
    if tools:
        cfg["tools"] = tools
    t_cfg = _resolve_thinking_config(model_id, thinking_level)
    if t_cfg is not None:
        cfg["thinking_config"] = t_cfg
    return types.GenerateContentConfig(**cfg)


def _extract_response_parts(response: Any) -> Tuple[str, List[Dict[str, Any]], List[Any]]:
    text_parts: List[str] = []
    calls: List[Dict[str, Any]] = []
    raw_parts: List[Any] = []
    candidates = getattr(response, "candidates", None) or []
    if candidates and getattr(candidates[0], "content", None):
        raw_parts = list(candidates[0].content.parts or [])
        for part in raw_parts:
            txt = getattr(part, "text", None)
            if txt and not getattr(part, "thought", False):
                text_parts.append(str(txt))
            fc = getattr(part, "function_call", None)
            if fc:
                args_dict = dict(fc.args) if fc.args else {}
                entry: Dict[str, Any] = {"name": fc.name, "args": args_dict}
                fc_id = getattr(fc, "id", None)
                if fc_id:
                    entry["id"] = fc_id
                calls.append(entry)
    if not text_parts and not calls and getattr(response, "text", None):
        text_parts.append(str(response.text))
    return "\n".join(text_parts).strip(), calls, raw_parts


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


def _build_scout_read_tools() -> Any:
    decls = [
        types.FunctionDeclaration(
            name="list_tree",
            description="List files and folders in a project directory.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "subpath": types.Schema(type=types.Type.STRING, description="Relative folder, default '.'"),
                    "max_depth": types.Schema(type=types.Type.INTEGER, description="Depth 1-4, default 3"),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="extract_headers",
            description="Extract module docstrings/header comments and top-level class/function symbols across matching files.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "subpath": types.Schema(type=types.Type.STRING, description="Relative folder/file, default '.'"),
                    "path_glob": types.Schema(type=types.Type.STRING, description="Optional glob e.g. '*.py' or 'src/**'"),
                    "max_files": types.Schema(type=types.Type.INTEGER, description="Max files to summarize (default 50)"),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="search_code",
            description="Search files and filenames by substring or regex.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "pattern": types.Schema(type=types.Type.STRING, description="Substring or regex pattern"),
                    "is_regex": types.Schema(type=types.Type.BOOLEAN, description="Regex mode"),
                    "ignore_case": types.Schema(type=types.Type.BOOLEAN, description="Ignore case (default true)"),
                    "subpath": types.Schema(type=types.Type.STRING, description="Optional relative folder or file"),
                    "path_glob": types.Schema(type=types.Type.STRING, description="Optional glob filter"),
                    "context_lines": types.Schema(type=types.Type.INTEGER, description="Context lines (default 2)"),
                },
                required=["pattern"],
            ),
        ),
        types.FunctionDeclaration(
            name="read_lines",
            description="Read an inclusive 1-based line range from a file (max 220 lines per call).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative file path"),
                    "start_line": types.Schema(type=types.Type.INTEGER, description="Start line (1-based)"),
                    "end_line": types.Schema(type=types.Type.INTEGER, description="End line (1-based)"),
                },
                required=["path", "start_line", "end_line"],
            ),
        ),
        types.FunctionDeclaration(
            name="read_file",
            description="Read a small file (truncated cleanly above ~24KB; prefer read_lines for large files).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING, description="Relative file path"),
                },
                required=["path"],
            ),
        ),
    ]
    return types.Tool(function_declarations=decls)


def _execute_scout_read_tool(project: ProjectProfile, name: str, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    root = project.root_path
    ignores = project.ignore_patterns
    if name == "list_tree":
        sub = str(args.get("subpath") or ".")
        depth = max(1, min(4, int(args.get("max_depth", 3))))
        res = fs_tools.list_tree(root, subpath=sub, max_depth=depth, extra_ignores=ignores, max_entries=350)
        tree_txt = res["tree"]
        if len(tree_txt) > 12_000:
            tree_txt = tree_txt[:12_000] + "\n... [truncated]"
        return {"subpath": sub, "tree": tree_txt}, f"{len(res['entries'])} entries in {sub}"

    if name == "extract_headers":
        sub = str(args.get("subpath") or ".")
        glob_pat = str(args.get("path_glob") or "").strip() or None
        max_files = max(1, min(60, int(args.get("max_files", 40))))
        res = fs_tools.extract_headers(
            root, subpath=sub, path_glob=glob_pat, include_symbols=True, max_files=max_files, extra_ignores=ignores
        )
        fmt = res["formatted"]
        if len(fmt) > 16_000:
            fmt = fmt[:16_000] + "\n... [truncated]"
        return {"file_count": res["file_count"], "headers": fmt}, f"{res['file_count']} files indexed"

    if name == "search_code":
        pattern = str(args.get("pattern") or "")
        sub = str(args.get("subpath") or "").strip() or None
        glob_pat = str(args.get("path_glob") or "").strip() or None
        res = fs_tools.search_code(
            root,
            pattern=pattern,
            is_regex=bool(args.get("is_regex", False)),
            path_glob=glob_pat,
            context_lines=max(0, min(4, int(args.get("context_lines", 2)))),
            max_matches=40,
            extra_ignores=ignores,
            ignore_case=bool(args.get("ignore_case", True)),
            subpath=sub,
        )
        fmt = res["formatted"]
        if len(fmt) > 16_000:
            fmt = fmt[:16_000] + "\n... [truncated]"
        return {
            "pattern": pattern,
            "total_hits": res["total_hits"],
            "shown_hits": res["shown_hits"],
            "matches": fmt,
        }, f"{res['total_hits']} hits for '{pattern}'"

    if name == "read_lines":
        path = str(args.get("path") or "")
        s = max(1, int(args.get("start_line", 1)))
        e = min(s + 219, max(s, int(args.get("end_line", s + 120))))
        res = fs_tools.read_lines(root, path, s, e)
        content = res["content"]
        if len(content) > SCOUT_MAX_TOOL_READ_CHARS:
            content = content[:SCOUT_MAX_TOOL_READ_CHARS] + "\n... [truncated]"
        return {
            "path": path,
            "start_line": res["start_line"],
            "end_line": res["end_line"],
            "total_lines": res["total_lines"],
            "content": content,
        }, f"{path}:{res['start_line']}-{res['end_line']}"

    if name == "read_file":
        path = str(args.get("path") or "")
        res = fs_tools.read_file(root, path, max_chars=SCOUT_MAX_TOOL_READ_CHARS)
        return {
            "path": path,
            "total_lines": res["total_lines"],
            "truncated": res["truncated"],
            "content": res["content"],
        }, f"{path} ({res['total_lines']}L)"

    raise ValueError(f"Tool '{name}' is not allowed inside read-only Local Scout.")


def _get_candidate_models(gemini: Any) -> List[str]:
    try:
        catalog = gemini.get_model_catalog(force_refresh=False, probe=False)
    except Exception:
        catalog = model_catalog.get_fallback_catalog()
    candidates = model_catalog.rank_light_scout_models(
        catalog,
        cooldowns=_snapshot_cooldowns(),
        allow_non_lite_fallback=False,
    )
    return candidates


def get_telemetry_snapshot(
    db: Any,
    gemini: Any = None,
    *,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    if db is None or not hasattr(db, "get_rate_telemetry"):
        return {}
    scout_models: List[str] = []
    if gemini is not None:
        try:
            scout_models = _get_candidate_models(gemini)
        except Exception:
            scout_models = []
    try:
        return db.get_rate_telemetry(model=model, scout_models=scout_models or None)
    except TypeError:
        return db.get_rate_telemetry(model=model)


def _generate_with_model_hop(
    gemini: Any,
    db: Any,
    *,
    contents: Any,
    system_instruction: str,
    tools: Optional[List[Any]] = None,
    thinking_level: str = "LOW",
    preferred_models: Optional[List[str]] = None,
    on_hop: Optional[Callable[[str, str, str], None]] = None,
) -> Tuple[Any, str, List[str]]:
    """
    Executes `client.models.generate_content` with automatic hopping across lightweight `-lite` models.
    Returns `(response, model_used, models_hopped_from)`.
    """
    client = gemini._get_client()
    candidates = list(preferred_models or _get_candidate_models(gemini))
    if not candidates:
        raise RuntimeError("No usable lightweight (-lite) Gemini models are currently available (all cooling down).")

    hopped_from: List[str] = []
    last_exc: Optional[Exception] = None

    for idx, model_id in enumerate(candidates):
        cfg = _build_scout_config(system_instruction, model_id, tools=tools, thinking_level=thinking_level)
        try:
            resp = client.models.generate_content(
                model=model_id,
                contents=contents,
                config=cfg,
            )
            usage = _extract_usage(resp)
            if db is not None:
                try:
                    db.record_api_usage(model_id, usage, source="scout")
                except TypeError:
                    try:
                        db.record_api_usage(model_id, usage)
                    except Exception:
                        pass
                except Exception:
                    pass
            return resp, model_id, hopped_from
        except Exception as exc:
            last_exc = exc
            err_lower = str(exc).lower()
            # Graceful retry with LOW (if MINIMAL rejected) or without thinking_config on 400 thinking errors
            if "thinking" in err_lower and "400" in err_lower:
                retry_levels = ["LOW", "OFF"] if (thinking_level or "").upper() == "MINIMAL" else ["OFF"]
                for fallback_lvl in retry_levels:
                    try:
                        fallback_cfg = _build_scout_config(
                            system_instruction,
                            model_id,
                            tools=tools,
                            thinking_level=fallback_lvl,
                        )
                        resp = client.models.generate_content(
                            model=model_id,
                            contents=contents,
                            config=fallback_cfg,
                        )
                        usage = _extract_usage(resp)
                        if db is not None:
                            try:
                                db.record_api_usage(model_id, usage, source="scout")
                            except TypeError:
                                try:
                                    db.record_api_usage(model_id, usage)
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        return resp, model_id, hopped_from
                    except Exception as exc2:
                        exc = exc2
                        last_exc = exc2

            if hasattr(gemini, "record_runtime_error"):
                try:
                    gemini.record_runtime_error(model_id, exc)
                except Exception:
                    pass
            _mark_cooldown(model_id, SCOUT_COOLDOWN_SECONDS)
            hopped_from.append(model_id)
            if idx + 1 < len(candidates) and on_hop is not None:
                on_hop(model_id, candidates[idx + 1], str(exc))

    raise RuntimeError(f"All lightweight Scout models failed or hit rate limits: {last_exc}")


_SCOUT_SYSTEM_PROMPT = """You are Local Scout, a fast read-only codebase investigator.
You operate inside a fresh stateless session with a strict step and token budget.
RULES:
1. Only cite file paths, line numbers, and symbol names you actually observed in tool outputs during THIS run. Never invent or guess file contents.
2. Start broad if unsure (`extract_headers`, `list_tree`, or `search_code`), then read exact ranges with `read_lines`. Batch related tool calls together!
3. Keep your final answer concise, structured, and factual with exact `path:line` citations and brief relevant snippets so the primary coding AI can act immediately."""


def run_scout_workspace(
    gemini: Any,
    db: Any,
    project: ProjectProfile,
    *,
    session_id: str,
    goal: str,
    subpath: str = ".",
    max_steps: int = SCOUT_DEFAULT_MAX_STEPS,
    max_tokens: int = SCOUT_DEFAULT_MAX_TOKENS,
    app: Any = None,
    event_loop: Optional[asyncio.AbstractEventLoop] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Runs a fresh, stateless, bounded read-only inspection loop using the lightest `-lite` models.
    Emits live WebSocket events (`scout_start`, `scout_step`, `scout_hop`, `scout_paused`, `scout_done`)
    and supports cooperative Pause/Resume/Stop via `ScoutRunController`.
    """
    rid = run_id or uuid.uuid4().hex[:10]
    step_cap = max(1, min(12, int(max_steps or SCOUT_DEFAULT_MAX_STEPS)))
    token_cap = max(4_000, min(120_000, int(max_tokens or SCOUT_DEFAULT_MAX_TOKENS)))
    scope = (subpath or ".").strip() or "."
    ctrl = ScoutRunController(rid, session_id, goal, step_cap)
    register_scout_run(ctrl)

    candidates = _get_candidate_models(gemini)
    active_model = candidates[0] if candidates else "gemini-3.1-flash-lite"

    _emit_ws(
        app,
        event_loop,
        {
            "type": "scout_start",
            "run_id": rid,
            "session_id": session_id,
            "goal": goal,
            "subpath": scope,
            "model": active_model,
            "max_steps": step_cap,
            "max_tokens": token_cap,
        },
    )

    tools_decl = [_build_scout_read_tools()]
    user_prompt = (
        f"Project: {project.name}\n"
        f"Scope hint: {scope}\n"
        f"Goal: {goal}\n\n"
        "Inspect the workspace using the read-only tools and return a concise, cited answer."
    )
    contents: List[Any] = [_make_text_content("user", user_prompt)]

    steps_trace: List[Dict[str, Any]] = []
    ledger_lines: List[str] = []
    hops: List[str] = []
    total_tokens_used = 0
    final_answer = ""
    stopped_early = False
    hit_budget = False
    current_model_idx = 0

    def _emit_pause(is_paused: bool) -> None:
        _emit_ws(
            app,
            event_loop,
            {
                "type": "scout_paused",
                "run_id": rid,
                "session_id": session_id,
                "paused": is_paused,
            },
        )

    def _on_hop(old_m: str, new_m: str, reason: str) -> None:
        nonlocal active_model
        active_model = new_m
        hops.append(f"{old_m} -> {new_m}")
        _emit_ws(
            app,
            event_loop,
            {
                "type": "scout_hop",
                "run_id": rid,
                "session_id": session_id,
                "from_model": old_m,
                "to_model": new_m,
                "reason": reason[:160],
            },
        )

    try:
        step_num = 0
        while step_num < step_cap:
            if not ctrl.wait_if_paused(_emit_pause):
                stopped_early = True
                break
            if total_tokens_used >= token_cap:
                hit_budget = True
                break

            step_num += 1
            preferred = candidates[current_model_idx:] if candidates else []

            def _hop_rebuild_and_notify(old_m: str, new_m: str, reason: str) -> None:
                _on_hop(old_m, new_m, reason)

            try:
                resp, used_model, failed_models = _generate_with_model_hop(
                    gemini,
                    db,
                    contents=contents,
                    system_instruction=_SCOUT_SYSTEM_PROMPT,
                    tools=tools_decl,
                    thinking_level="LOW",
                    preferred_models=preferred,
                    on_hop=_hop_rebuild_and_notify,
                )
            except Exception:
                # If a mid-loop hop failed due to model-specific thought_signature parts, rebuild clean text ledger & retry once
                if ledger_lines:
                    clean_recap = (
                        f"{user_prompt}\n\n"
                        "Findings collected so far (continue cleanly without repeating completed reads):\n"
                        + "\n".join(ledger_lines[-12:])
                    )
                    contents = [_make_text_content("user", clean_recap)]
                    candidates = _get_candidate_models(gemini)
                    current_model_idx = 0
                    resp, used_model, failed_models = _generate_with_model_hop(
                        gemini,
                        db,
                        contents=contents,
                        system_instruction=_SCOUT_SYSTEM_PROMPT,
                        tools=tools_decl,
                        thinking_level="LOW",
                        preferred_models=candidates,
                        on_hop=_hop_rebuild_and_notify,
                    )
                else:
                    raise

            active_model = used_model
            if used_model in candidates:
                current_model_idx = candidates.index(used_model)
            usage = _extract_usage(resp)
            total_tokens_used += int(usage.total_tokens or (usage.input_tokens + usage.output_tokens + usage.thinking_tokens))

            text, fcalls, raw_parts = _extract_response_parts(resp)

            if failed_models and ledger_lines:
                # Normalize history after a hop so next turn doesn't send old model's thought_signatures
                contents = [
                    _make_text_content(
                        "user",
                        f"{user_prompt}\n\nEvidence collected before model hop:\n" + "\n".join(ledger_lines[-12:]),
                    )
                ]

            if not fcalls:
                final_answer = text or "(Scout finished without extra output.)"
                break

            # Append model tool call turn cleanly
            if raw_parts and hasattr(types, "Content"):
                try:
                    contents.append(types.Content(role="model", parts=raw_parts))
                except Exception:
                    contents.append({"role": "model", "parts": [{"text": text or "calling tools"}]})
            else:
                contents.append({"role": "model", "parts": [{"text": text or "calling tools"}]})

            fr_parts: List[Any] = []
            for fc in fcalls:
                if not ctrl.wait_if_paused(_emit_pause):
                    stopped_early = True
                    break

                tname = fc["name"]
                targs = fc.get("args") or {}
                try:
                    result_obj, summary_str = _execute_scout_read_tool(project, tname, targs)
                    status_flag = "ok"
                except Exception as exc:
                    result_obj = {"error": str(exc)}
                    summary_str = f"Error: {exc}"
                    status_flag = "err"

                step_rec = {
                    "step": step_num,
                    "tool": tname,
                    "args": targs,
                    "summary": summary_str,
                    "status": status_flag,
                }
                steps_trace.append(step_rec)
                ledger_lines.append(f"- Step {step_num}: {tname}({json.dumps(targs, ensure_ascii=False)}) -> {summary_str}")

                _emit_ws(
                    app,
                    event_loop,
                    {
                        "type": "scout_step",
                        "run_id": rid,
                        "session_id": session_id,
                        "step": step_num,
                        "max_steps": step_cap,
                        "tool": tname,
                        "summary": summary_str,
                        "status": status_flag,
                        "model": active_model,
                        "tokens_used": total_tokens_used,
                    },
                )

                if hasattr(types, "Part") and hasattr(types, "FunctionResponse"):
                    try:
                        fr_kw: Dict[str, Any] = {"name": tname, "response": result_obj}
                        if fc.get("id"):
                            fr_kw["id"] = fc["id"]
                        fr_parts.append(types.Part(function_response=types.FunctionResponse(**fr_kw)))
                    except Exception:
                        fr_parts.append({"function_response": {"name": tname, "response": result_obj}})
                else:
                    fr_parts.append({"function_response": {"name": tname, "response": result_obj}})

            if fr_parts:
                if hasattr(types, "Content"):
                    try:
                        contents.append(types.Content(role="user", parts=fr_parts))
                    except Exception:
                        contents.append({"role": "user", "parts": fr_parts})
                else:
                    contents.append({"role": "user", "parts": fr_parts})

            if stopped_early:
                break

        # If stopped early, hit step cap, or hit token cap without final synthesis, do 1 final synthesis call (text only, no tools)
        if not final_answer:
            if not stopped_early and step_num >= step_cap:
                hit_budget = True
            synth_prompt = (
                "Stop calling tools and synthesize ONLY verified facts observed above into a concise cited answer."
                if not stopped_early
                else "Stopped early by user. Summarize ONLY the verified findings observed so far with exact file/line citations."
            )
            contents.append(_make_text_content("user", synth_prompt))
            try:
                synth_resp, used_m, _ = _generate_with_model_hop(
                    gemini,
                    db,
                    contents=contents,
                    system_instruction=_SCOUT_SYSTEM_PROMPT,
                    tools=None,
                    thinking_level="MINIMAL",
                    preferred_models=_get_candidate_models(gemini),
                    on_hop=_on_hop,
                )
                active_model = used_m
                u2 = _extract_usage(synth_resp)
                total_tokens_used += int(u2.total_tokens or (u2.input_tokens + u2.output_tokens + u2.thinking_tokens))
                final_answer, _, _ = _extract_response_parts(synth_resp)
            except Exception:
                final_answer = "Partial evidence collected:\n" + ("\n".join(ledger_lines) if ledger_lines else "(None)")

        if stopped_early and not final_answer.startswith("[Stopped"):
            final_answer = f"[Stopped early by user]\n{final_answer}"
        elif hit_budget and not stopped_early:
            final_answer = f"[Budget cap reached — partial synthesis]\n{final_answer}"

        out = {
            "run_id": rid,
            "goal": goal,
            "subpath": scope,
            "model_used": active_model,
            "models_hopped": hops,
            "steps_taken": len(steps_trace),
            "steps": steps_trace,
            "tokens_used": total_tokens_used,
            "stopped_early": stopped_early,
            "answer": final_answer,
        }
        _emit_ws(
            app,
            event_loop,
            {
                "type": "scout_done",
                "run_id": rid,
                "session_id": session_id,
                "model": active_model,
                "steps_taken": len(steps_trace),
                "tokens_used": total_tokens_used,
                "stopped_early": stopped_early,
            },
        )
        return out
    finally:
        unregister_scout_run(rid)


def summarize_command_output(
    gemini: Any,
    db: Any,
    *,
    command: str,
    raw_output: str,
    exit_code: int,
    focus: Optional[str] = None,
    exec_id: Optional[str] = None,
    threshold_chars: int = OUTPUT_SUMMARIZE_THRESHOLD_CHARS,
) -> Optional[Dict[str, Any]]:
    """
    Uses a fresh single call on the lightest available `-lite` model to summarize long command/test outputs
    (or answer a targeted `focus` question). Returns None on fallback so callers degrade cleanly.
    """
    text = (raw_output or "").strip()
    if not text:
        return None
    has_focus = bool((focus or "").strip())
    if not has_focus and len(text) <= max(500, int(threshold_chars or OUTPUT_SUMMARIZE_THRESHOLD_CHARS)):
        return None

    # Cap input sent to the light summarizer to preserve free-tier TPM/context
    excerpt_for_ai = text if len(text) <= 45_000 else (text[:15_000] + "\n\n... [middle omitted] ...\n\n" + text[-28_000:])
    focus_clause = (
        f"Answer this specific question from the main AI: {focus.strip()}\n"
        if has_focus
        else "Summarize test/script counts, key outcomes, warnings, and actionable findings concisely.\n"
    )
    prompt = (
        f"Command: `{command}` (exit_code={exit_code}, exec_id={exec_id or 'n/a'})\n"
        f"{focus_clause}"
        "Only report facts present in the output. Quote exact numbers/symbols/paths. Keep under 6 lines, no advice.\n\n"
        f"```text\n{excerpt_for_ai}\n```"
    )
    try:
        resp, model_used, _ = _generate_with_model_hop(
            gemini,
            db,
            contents=prompt,
            system_instruction="Extract facts from local CLI/test output in <= 6 short lines. No tutorials or suggestions.",
            thinking_level="MINIMAL",
        )
        summary_text, _, _ = _extract_response_parts(resp)
        if not summary_text:
            return None
        return {
            "summary": summary_text,
            "model_used": model_used,
            "tokens_used": _extract_usage(resp).total_tokens,
        }
    except Exception:
        return None


def triage_command_failure(
    gemini: Any,
    db: Any,
    project: ProjectProfile,
    *,
    command: str,
    raw_output: str,
    exit_code: int,
    focus: Optional[str] = None,
    exec_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Extracts the essential failing signal from noisy command/test output while Scout is enabled.
    Allows up to 2 read-only file/line checks ONLY if needed to identify what failed,
    and returns a terse factual digest without resolution essays.
    """
    text = (raw_output or "").strip()
    if not text:
        return None

    tail_or_slice = text if len(text) <= 28_000 else (text[:6_000] + "\n...\n" + text[-20_000:])
    focus_note = f"\nRequested focus: {focus.strip()}" if (focus or "").strip() else ""
    prompt = (
        f"Command failed: `{command}` (exit_code={exit_code}, exec_id={exec_id or 'n/a'}).{focus_note}\n"
        "Extract ONLY the essential failure(s): which test/step failed, exact `file:line`, and the exact error message. "
        "Keep under 5 lines. Do NOT write Resolution sections, advice, or multi-paragraph explanations. "
        "Only call `read_lines` if the traceback line itself lacks context needed to locate what failed.\n\n"
        f"```text\n{tail_or_slice}\n```"
    )
    tools_decl = [_build_scout_read_tools()]
    contents: List[Any] = [_make_text_content("user", prompt)]
    total_tokens = 0
    active_model = ""

    try:
        for _step in range(2):
            resp, model_used, _ = _generate_with_model_hop(
                gemini,
                db,
                contents=contents,
                system_instruction="Extract exact failure lines and test names concisely (<= 5 lines). Never write tutorials or resolution steps.",
                tools=tools_decl,
                thinking_level="MINIMAL",
            )
            active_model = model_used
            total_tokens += int(_extract_usage(resp).total_tokens or 0)
            txt, fcalls, raw_parts = _extract_response_parts(resp)
            if not fcalls:
                if txt:
                    return {"summary": txt, "model_used": active_model, "tokens_used": total_tokens}
                return None

            if raw_parts and hasattr(types, "Content"):
                contents.append(types.Content(role="model", parts=raw_parts))
            else:
                contents.append({"role": "model", "parts": [{"text": txt or "inspecting failure"}]})

            fr_parts: List[Any] = []
            for fc in fcalls[:3]:
                try:
                    res_obj, _ = _execute_scout_read_tool(project, fc["name"], fc.get("args") or {})
                except Exception as exc:
                    res_obj = {"error": str(exc)}
                if hasattr(types, "Part") and hasattr(types, "FunctionResponse"):
                    try:
                        fr_parts.append(types.Part(function_response=types.FunctionResponse(name=fc["name"], response=res_obj)))
                        continue
                    except Exception:
                        pass
                fr_parts.append({"function_response": {"name": fc["name"], "response": res_obj}})
            contents.append(types.Content(role="user", parts=fr_parts) if hasattr(types, "Content") else {"role": "user", "parts": fr_parts})

        # Finalize after 2 tool hops
        contents.append(_make_text_content("user", "Output ONLY the <= 5-line failure summary now (no tools, no advice)."))
        resp, model_used, _ = _generate_with_model_hop(
            gemini,
            db,
            contents=contents,
            system_instruction="Extract exact failure lines concisely (<= 5 lines). Never write tutorials.",
            tools=None,
            thinking_level="MINIMAL",
        )
        active_model = model_used
        total_tokens += int(_extract_usage(resp).total_tokens or 0)
        txt, _, _ = _extract_response_parts(resp)
        return {"summary": txt, "model_used": active_model, "tokens_used": total_tokens} if txt else None
    except Exception:
        return None


def draft_header_updates(
    gemini: Any,
    db: Any,
    project: ProjectProfile,
    *,
    paths: List[str],
    max_files: int = 8,
) -> Dict[str, Any]:
    """
    Uses a fresh `-lite` Scout call per target file to generate an updated top-of-file header docstring/comment
    matching the file's actual code, returning prepared unified diff patches (never modifying disk directly).
    """
    clean_paths = []
    seen = set()
    for p in paths:
        norm = str(p or "").strip()
        if norm and norm not in seen:
            seen.add(norm)
            clean_paths.append(norm)
    clean_paths = clean_paths[: max(1, min(10, int(max_files or 8)))]
    if not clean_paths:
        raise ValueError("update_header requires at least one relative file path.")

    proposed: List[Dict[str, Any]] = []
    total_tokens = 0
    used_models: List[str] = []

    for rel in clean_paths:
        finfo = fs_tools.read_file(project.root_path, rel, max_chars=32_000)
        raw = finfo["raw_content"]
        suffix = Path(rel).suffix.lower()
        lang_hint = "Python module docstring (triple quotes \"\"\"...\"\"\")" if suffix == ".py" else (
            "HTML comment <!-- ... -->" if suffix in (".html", ".htm") else "leading /* ... */ or // comment block"
        )
        prompt = (
            f"File: `{rel}` ({finfo['total_lines']} lines)\n"
            f"Write ONLY an accurate, concise top-of-file header comment in {lang_hint} style (2-6 lines max) "
            "summarizing what this file actually implements now. Do NOT include code, markdown fences, or prose.\n\n"
            f"```text\n{finfo['content']}\n```"
        )
        resp, model_used, _ = _generate_with_model_hop(
            gemini,
            db,
            contents=prompt,
            system_instruction="You write concise, accurate source-file header comments only. Never emit markdown code fences.",
            thinking_level="MINIMAL",
        )
        if model_used not in used_models:
            used_models.append(model_used)
        total_tokens += int(_extract_usage(resp).total_tokens or 0)
        header_txt, _, _ = _extract_response_parts(resp)
        header_txt = _strip_markdown_fence(header_txt).strip()
        if not header_txt:
            continue

        updated_content = _replace_or_insert_file_header(raw, suffix, header_txt)
        if updated_content == raw:
            continue
        preview = patch_tools.preview_full_write(project.root_path, rel, updated_content)
        proposed.append(
            {
                "path": rel,
                "content": updated_content,
                "diff": preview["diff"],
                "ast_valid": preview["ast_valid"],
                "estimated_tokens": preview["estimated_tokens"],
            }
        )

    return {
        "proposed_patches": proposed,
        "models_used": used_models,
        "tokens_used": total_tokens,
    }


def _strip_markdown_fence(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines)
    return s


def _replace_or_insert_file_header(raw: str, suffix: str, new_header: str) -> str:
    """Replaces an existing top-of-file docstring/comment or inserts the new header cleanly."""
    lines = raw.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in raw else "\n"

    if suffix == ".py":
        h = new_header.strip()
        if not (h.startswith('"""') or h.startswith("'''")):
            h = f'"""\n{h}\n"""'
        idx = 0
        prefix_lines: List[str] = []
        while idx < len(lines) and (lines[idx].startswith("#!") or "coding" in lines[idx][:40] and lines[idx].lstrip().startswith("#")):
            prefix_lines.append(lines[idx])
            idx += 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        # Check for existing module docstring
        if idx < len(lines) and lines[idx].lstrip().startswith(('"""', "'''")):
            quote = '"""' if lines[idx].lstrip().startswith('"""') else "'''"
            rest = lines[idx].lstrip()[3:]
            if quote in rest:
                idx += 1
            else:
                idx += 1
                while idx < len(lines):
                    if quote in lines[idx]:
                        idx += 1
                        break
                    idx += 1
            while idx < len(lines) and not lines[idx].strip():
                idx += 1
        elif idx < len(lines) and lines[idx].lstrip().startswith("#"):
            while idx < len(lines) and lines[idx].lstrip().startswith("#"):
                idx += 1
            while idx < len(lines) and not lines[idx].strip():
                idx += 1
        body = "".join(lines[idx:])
        head_block = "".join(prefix_lines) + h + newline + (newline if body else "")
        return head_block + body

    if suffix in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".go", ".rs", ".java"):
        h = new_header.strip()
        if not (h.startswith("//") or h.startswith("/*")):
            h = "\n".join(f"// {ln}" if ln.strip() else "//" for ln in h.splitlines())
        idx = 0
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx < len(lines) and lines[idx].lstrip().startswith("/*"):
            while idx < len(lines):
                has_end = "*/" in lines[idx]
                idx += 1
                if has_end:
                    break
            while idx < len(lines) and not lines[idx].strip():
                idx += 1
        elif idx < len(lines) and lines[idx].lstrip().startswith("//"):
            while idx < len(lines) and lines[idx].lstrip().startswith("//"):
                idx += 1
            while idx < len(lines) and not lines[idx].strip():
                idx += 1
        body = "".join(lines[idx:])
        return h + newline + (newline if body else "") + body

    if suffix in (".html", ".htm"):
        h = new_header.strip()
        if not h.startswith("<!--"):
            h = f"<!-- {h} -->"
        idx = 0
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx < len(lines) and lines[idx].lstrip().startswith("<!--"):
            while idx < len(lines):
                has_end = "-->" in lines[idx]
                idx += 1
                if has_end:
                    break
        body = "".join(lines[idx:])
        return h + newline + body

    return raw


def draft_git_commit_with_scout(gemini: Any, db: Any, diff_text: str) -> Dict[str, Any]:
    """Drafts a git commit message via lightweight `-lite` model hopping."""
    from services.gemini_client import get_git_commit_prompt

    if not diff_text.strip():
        return {"message": "chore: update workspace", "model_used": "", "usage": TokenStats()}

    trimmed_diff = diff_text if len(diff_text) <= 45_000 else (diff_text[:45_000] + "\n... [diff truncated]")
    prompt = get_git_commit_prompt(trimmed_diff)
    resp, model_used, _ = _generate_with_model_hop(
        gemini,
        db,
        contents=prompt,
        system_instruction="Write a concise, accurate git commit message. Return only the commit message text without code fences.",
        thinking_level="MINIMAL",
    )
    txt, _, _ = _extract_response_parts(resp)
    txt = _strip_markdown_fence(txt).strip()
    return {
        "message": txt or "chore: apply updates",
        "model_used": model_used,
        "usage": _extract_usage(resp),
    }
