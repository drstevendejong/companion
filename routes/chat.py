"""
routes/chat.py - Human-gated Gemini chat loop and tool approval/denial/edit-range endpoints.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from google.genai import types

from config import OUTPUT_FAILURE_TRIAGE_MIN_CHARS

from models import (
    BatchGateDecisionRequest,
    ChatRetryRequest,
    ChatSendRequest,
    MessageRecord,
    ProjectProfile,
    SessionInfo,
    SessionUpdate,
    ToolGateDecision,
)
from services.key_store import has_configured_api_key
from services import bridge as bridge_svc
from services import checkpoints as cp_svc
from services import fs_tools, local_scout, patch_tools, runner
from services.gemini_client import (
    READ_ONLY_TOOLS,
    SCOUT_TOOLS,
    build_user_parts,
    prepare_tool_gate_preview,
)
from services.quota_parser import analyze_gemini_error, format_gemini_error

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _get_effective_scout_settings(db: Any, gemini: Any) -> Dict[str, Any]:
    has_key = has_configured_api_key(gemini) if gemini is not None else False
    if hasattr(db, "get_scout_settings"):
        return db.get_scout_settings(has_key=has_key)
    return {
        "enabled": False,
        "configured_enabled": False,
        "auto_approve": False,
        "max_tokens": 30_000,
        "summarize_threshold_chars": 4_000,
    }


def _maybe_append_scout_notice(db: Any, session: SessionInfo, scout_enabled: bool, text: str) -> str:
    """Appends an explicit Scout ON/OFF notice once whenever Scout state changes in Bridge mode."""
    target_bit = 1 if scout_enabled else 0
    if getattr(session, "scout_announced_state", 0) == target_bit:
        return text
    notice = bridge_svc.build_scout_state_notice(scout_enabled)
    if hasattr(db, "update_session"):
        db.update_session(session.id, SessionUpdate(scout_announced_state=target_bit))
    return f"{text}\n\n{notice}".strip() if text else notice


async def _maybe_summarize_or_triage_output(
    *,
    gemini: Any,
    db: Any,
    project: ProjectProfile,
    scout_cfg: Dict[str, Any],
    command: str,
    raw_output: str,
    exit_code: int,
    passed: bool,
    focus: str | None,
    exec_id: str | None,
) -> Dict[str, Any]:
    if not scout_cfg.get("enabled") or gemini is None or not raw_output.strip():
        return {}
    threshold = int(scout_cfg.get("summarize_threshold_chars") or 4_000)
    has_focus = bool((focus or "").strip())
    if not passed:
        if not has_focus and len(raw_output) <= OUTPUT_FAILURE_TRIAGE_MIN_CHARS:
            return {}
        res = await asyncio.to_thread(
            local_scout.triage_command_failure,
            gemini,
            db,
            project,
            command=command,
            raw_output=raw_output,
            exit_code=exit_code,
            focus=focus,
            exec_id=exec_id,
        )
        return res or {}
    if has_focus or len(raw_output) > threshold:
        res = await asyncio.to_thread(
            local_scout.summarize_command_output,
            gemini,
            db,
            command=command,
            raw_output=raw_output,
            exit_code=exit_code,
            focus=focus,
            exec_id=exec_id,
            threshold_chars=threshold,
        )
        return res or {}
    return {}


def _trailing_bridge_reply(db: Any, session_id: str) -> str:
    return bridge_svc.format_trailing_tool_events(db.list_messages(session_id))


def _messages_to_history(messages: List[MessageRecord]) -> List[types.Content]:
    """Reconstructs valid alternating Gemini Contents, merging adjacent tool_event/user parts."""
    contents: List[types.Content] = []
    pending_user_parts: List[types.Part] = []

    def _flush_user() -> None:
        if pending_user_parts:
            contents.append(types.Content(role="user", parts=list(pending_user_parts)))
            pending_user_parts.clear()

    for msg in messages:
        if msg.role == "user":
            pending_user_parts.extend(build_user_parts(msg.text, msg.attachments))
        elif msg.role == "model":
            _flush_user()
            parts: List[types.Part] = []
            if msg.text:
                parts.append(types.Part.from_text(text=msg.text))
            for tc in msg.tool_calls:
                fc_kwargs: Dict[str, Any] = {
                    "name": tc["name"],
                    "args": tc.get("args", {}),
                }
                if tc.get("id"):
                    fc_kwargs["id"] = tc["id"]
                part_kwargs: Dict[str, Any] = {
                    "function_call": types.FunctionCall(**fc_kwargs),
                }
                sig_b64 = tc.get("thought_signature_b64")
                if sig_b64:
                    part_kwargs["thought_signature"] = base64.b64decode(sig_b64)
                parts.append(types.Part(**part_kwargs))
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        elif msg.role == "tool_event":
            payload = json.loads(msg.text)
            fr_kwargs: Dict[str, Any] = {
                "name": payload["tool_name"],
                "response": payload["response"],
            }
            if payload.get("call_id"):
                fr_kwargs["id"] = payload["call_id"]
            pending_user_parts.append(
                types.Part(function_response=types.FunctionResponse(**fr_kwargs))
            )
    _flush_user()
    return contents


def _serialize_tool_response(tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    return bridge_svc.serialize_tool_response(tool_name, result)


def _advance_turn(
    db: Any,
    gemini: Any,
    session: SessionInfo,
    project: ProjectProfile,
    max_auto_loops: int = 5,
    app: Any = None,
) -> Dict[str, Any]:
    """Runs Gemini turn(s), auto-executing approved-threshold reads / auto-scout, pausing at gates."""
    auto_loops = 0
    while True:
        scout_cfg = _get_effective_scout_settings(db, gemini)
        scout_enabled = bool(scout_cfg.get("enabled"))
        scout_auto = bool(scout_cfg.get("auto_approve"))

        history = _messages_to_history(db.list_messages(session.id))
        turn = gemini.generate_turn(session, project, history, scout_enabled=scout_enabled)
        usage = turn["usage"]
        db.record_api_usage(session.model, usage)
        updated_session = db.add_session_tokens(session.id, usage) or session

        tool_calls = turn.get("function_calls") or []
        db.add_message(
            session.id,
            role="model",
            text=turn.get("text", ""),
            tool_calls=tool_calls,
            token_usage=usage,
        )

        if not tool_calls:
            break

        all_auto_approved = True
        prepared_items: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
        for tc in tool_calls:
            if tc["name"] in SCOUT_TOOLS and not scout_enabled:
                preview = {
                    "category": "error",
                    "title": f"Local AI Scout Disabled: {tc['name']}",
                    "error": f"{tc['name']} cannot run because Local AI Scout is currently OFF or lacks a configured API key.",
                    "estimated_tokens": 0,
                }
            else:
                try:
                    preview = prepare_tool_gate_preview(project, tc["name"], tc.get("args", {}))
                except Exception as exc:
                    preview = {
                        "category": "error",
                        "title": f"Tool Preparation Error: {tc['name']}",
                        "error": str(exc),
                        "estimated_tokens": 0,
                    }
            prepared_items.append((tc, preview))
            is_auto_read = (
                tc["name"] in READ_ONLY_TOOLS
                and preview.get("category") == "read"
                and updated_session.auto_approve_read_tokens > 0
                and preview.get("estimated_tokens", 0) <= updated_session.auto_approve_read_tokens
            )
            is_auto_scout = (
                tc["name"] == "scout_workspace"
                and scout_enabled
                and scout_auto
                and preview.get("category") == "scout"
            )
            if not (is_auto_read or is_auto_scout):
                all_auto_approved = False

        if all_auto_approved and auto_loops < max_auto_loops:
            auto_loops += 1
            for tc, preview in prepared_items:
                if tc["name"] == "scout_workspace":
                    scout_res = local_scout.run_scout_workspace(
                        gemini,
                        db,
                        project,
                        session_id=session.id,
                        goal=str(preview.get("goal") or ""),
                        subpath=str(preview.get("subpath") or "."),
                        max_steps=int(preview.get("max_steps") or 8),
                        max_tokens=int(scout_cfg.get("max_tokens") or 30_000),
                        app=app,
                        event_loop=getattr(getattr(app, "state", None), "event_loop", None),
                    )
                    compact = _serialize_tool_response("scout_workspace", scout_res)
                else:
                    compact = _serialize_tool_response(tc["name"], preview["prepared_result"])
                db.add_message(
                    session.id,
                    role="tool_event",
                    text=json.dumps({
                        "tool_name": tc["name"],
                        "call_id": tc.get("id"),
                        "response": compact,
                    }),
                )
            session = updated_session
            continue

        for tc, preview in prepared_items:
            if preview.get("category") == "error":
                args = tc.get("args") or {}
                target_label = str(
                    args.get("path")
                    or args.get("old_path")
                    or args.get("target")
                    or args.get("pattern")
                    or args.get("subpath")
                    or ""
                )
                db.add_message(
                    session.id,
                    role="tool_event",
                    text=json.dumps({
                        "tool_name": tc["name"],
                        "call_id": tc.get("id"),
                        "response": {"error": preview["error"], "target": target_label},
                    }),
                )
                continue

            ui_preview = {
                k: v for k, v in preview.items()
                if k not in ("prepared_result", "prepared_patch")
            }
            if tc.get("id"):
                ui_preview["call_id"] = tc["id"]
            db.create_gate(
                session_id=session.id,
                tool_name=tc["name"],
                args=tc.get("args", {}),
                preview=ui_preview,
            )
        break

    final_session = db.get_session(session.id) or session
    return {
        "session": final_session,
        "messages": db.list_messages(session.id),
        "pending_gates": db.list_pending_gates(session.id),
        "checkpoints": db.list_checkpoints(session.id) if hasattr(db, "list_checkpoints") else [],
        "telemetry": local_scout.get_telemetry_snapshot(db, gemini, model=final_session.model),
    }


def _build_gemini_http_exception(gemini: Any, model_id: str, exc: Exception) -> HTTPException:
    if hasattr(gemini, "record_runtime_error"):
        try:
            gemini.record_runtime_error(model_id, exc)
        except Exception:
            pass
    info = analyze_gemini_error(exc)
    status = 429 if info["is_429"] else (400 if info["is_auth"] else 500)
    return HTTPException(status_code=status, detail=format_gemini_error(exc))


@router.post("/send")
def send_chat_message(payload: ChatSendRequest, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    gemini = request.app.state.gemini

    session = db.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if not session.api_enabled:
        raise HTTPException(
            status_code=403,
            detail="This is a Playground Bridge session (API use disabled). Start a New Session with Gemini API enabled.",
        )
    project = db.get_project(session.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    if db.list_pending_gates(session.id):
        raise HTTPException(
            status_code=409,
            detail="Resolve pending tool approval gates before sending a new prompt.",
        )

    db.add_message(
        session.id,
        role="user",
        text=payload.prompt,
        attachments=payload.attachments,
    )

    try:
        return _advance_turn(db, gemini, session, project, app=getattr(request, "app", None))
    except Exception as exc:
        raise _build_gemini_http_exception(gemini, session.model, exc) from exc


@router.post("/scout/{run_id}/pause")
def pause_scout_run(run_id: str) -> Dict[str, Any]:
    ctrl = local_scout.get_scout_run(run_id)
    if ctrl is None:
        raise HTTPException(status_code=404, detail="Scout run is not active.")
    ctrl.pause()
    return {"ok": True, "run_id": run_id, "paused": True, "stop_requested": ctrl.stop_requested}


@router.post("/scout/{run_id}/resume")
def resume_scout_run(run_id: str) -> Dict[str, Any]:
    ctrl = local_scout.get_scout_run(run_id)
    if ctrl is None:
        raise HTTPException(status_code=404, detail="Scout run is not active.")
    ctrl.resume()
    return {"ok": True, "run_id": run_id, "paused": False, "stop_requested": ctrl.stop_requested}


@router.post("/scout/{run_id}/stop")
def stop_scout_run(run_id: str) -> Dict[str, Any]:
    ctrl = local_scout.get_scout_run(run_id)
    if ctrl is None:
        raise HTTPException(status_code=404, detail="Scout run is not active.")
    ctrl.stop()
    return {"ok": True, "run_id": run_id, "paused": False, "stop_requested": True}


@router.post("/retry")
def retry_last_turn(payload: ChatRetryRequest, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    gemini = request.app.state.gemini

    session = db.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if not session.api_enabled:
        raise HTTPException(
            status_code=403,
            detail="This is a Playground Bridge session (API use disabled).",
        )
    project = db.get_project(session.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    if db.list_pending_gates(session.id):
        raise HTTPException(
            status_code=409,
            detail="Resolve pending tool approval gates before retrying.",
        )

    messages = db.list_messages(session.id)
    if not messages or messages[-1].role == "model":
        raise HTTPException(
            status_code=400,
            detail="Nothing to retry—the latest turn already has a Gemini response.",
        )

    try:
        return _advance_turn(db, gemini, session, project, app=getattr(request, "app", None))
    except Exception as exc:
        raise _build_gemini_http_exception(gemini, session.model, exc) from exc


async def _execute_gate_decision(
    db: Any,
    gate: Dict[str, Any],
    decision: ToolGateDecision,
    session: SessionInfo,
    project: ProjectProfile,
    app: Any = None,
    gemini: Any = None,
) -> Dict[str, Any]:
    gemini_client = gemini or (getattr(getattr(app, "state", None), "gemini", None) if app is not None else None)
    scout_cfg = _get_effective_scout_settings(db, gemini_client)
    gate_id = gate["id"]
    tool_name = gate["tool_name"]
    args = gate["args"]
    extra_ui: Dict[str, Any] = {}
    call_id = (gate.get("preview") or {}).get("call_id")
    target_label = str(
        args.get("path")
        or args.get("old_path")
        or args.get("target")
        or args.get("pattern")
        or args.get("subpath")
        or ""
    )

    def _record_gate_error(exc: Exception) -> Dict[str, Any]:
        err_text = str(exc)
        err_resp = {"error": err_text, "target": target_label}
        db.resolve_gate(gate_id, "error", err_resp)
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": err_resp}),
        )
        prefix = f"{tool_name} ({target_label})" if target_label else tool_name
        return {"gate_error": f"{prefix}: {err_text}"}

    if decision.action == "deny":
        tool_resp = {
            "denied_by_user": True,
            "target": target_label,
            "reason": decision.reason or "User declined this action. Discuss or propose another approach.",
        }
        db.resolve_gate(gate_id, "denied", tool_resp)
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": tool_resp}),
        )

    elif tool_name in READ_ONLY_TOOLS:
        try:
            if decision.action == "edit_range" and tool_name in ("read_lines", "read_file"):
                s = decision.start_line or 1
                e = decision.end_line or (s + 150)
                res = fs_tools.read_lines(project.root_path, str(args["path"]), s, e)
                compact = _serialize_tool_response("read_lines", res)
            else:
                prepared = prepare_tool_gate_preview(project, tool_name, args)
                compact = _serialize_tool_response(tool_name, prepared["prepared_result"])
        except Exception as exc:
            return _record_gate_error(exc)

        db.resolve_gate(gate_id, "approved", {"bytes": len(json.dumps(compact))})
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": compact}),
        )

    elif tool_name == "scout_workspace":
        if not scout_cfg.get("enabled") or gemini_client is None:
            return _record_gate_error(
                RuntimeError("Local AI Scout is disabled or missing an API key.")
            )
        if decision.action == "approve_and_auto" and hasattr(db, "set_scout_settings"):
            db.set_scout_settings(auto_approve=True)
            extra_ui["scout_auto_approve_enabled"] = True

        goal = str(args.get("goal") or args.get("prompt") or "").strip()
        subpath = str(args.get("subpath") or ".").strip() or "."
        max_steps = max(1, min(12, int(args.get("max_steps") or 8)))
        try:
            loop = asyncio.get_running_loop()
            scout_res = await asyncio.to_thread(
                local_scout.run_scout_workspace,
                gemini_client,
                db,
                project,
                session_id=session.id,
                goal=goal,
                subpath=subpath,
                max_steps=max_steps,
                max_tokens=int(scout_cfg.get("max_tokens") or 30_000),
                app=app,
                event_loop=loop,
            )
        except Exception as exc:
            return _record_gate_error(exc)

        compact = _serialize_tool_response("scout_workspace", scout_res)
        extra_ui["scout_result"] = scout_res
        db.resolve_gate(
            gate_id,
            "approved",
            {
                "model_used": scout_res.get("model_used"),
                "steps_taken": scout_res.get("steps_taken"),
                "tokens_used": scout_res.get("tokens_used"),
            },
        )
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": compact}),
        )

    elif tool_name == "update_header":
        if not scout_cfg.get("enabled") or gemini_client is None:
            return _record_gate_error(
                RuntimeError("Local AI Scout is disabled or missing an API key.")
            )
        raw_paths = args.get("paths") or args.get("path") or []
        if isinstance(raw_paths, str):
            paths_list = [p.strip() for p in raw_paths.split(",") if p.strip()]
        else:
            paths_list = [str(p).strip() for p in raw_paths if str(p).strip()]
        try:
            hdr_res = await asyncio.to_thread(
                local_scout.draft_header_updates,
                gemini_client,
                db,
                project,
                paths=paths_list,
            )
        except Exception as exc:
            return _record_gate_error(exc)

        staged_paths: List[str] = []
        created_gate_ids: List[str] = []
        for patch in hdr_res.get("proposed_patches") or []:
            p_args = {
                "path": patch["path"],
                "content": patch["content"],
                "summary": f"Update header comment ({patch['path']}) via Local AI Scout",
            }
            p_preview = prepare_tool_gate_preview(project, "write_file", p_args)
            ui_preview = {
                k: v for k, v in p_preview.items()
                if k not in ("prepared_result", "prepared_patch")
            }
            g = db.create_gate(
                session_id=session.id,
                tool_name="write_file",
                args=p_args,
                preview=ui_preview,
            )
            staged_paths.append(patch["path"])
            created_gate_ids.append(g["id"])

        unchanged = [p for p in paths_list if p not in staged_paths]
        tool_resp = {
            "staged_count": len(staged_paths),
            "staged_paths": staged_paths,
            "unchanged_paths": unchanged,
            "pending_gate_ids": created_gate_ids,
            "model": (hdr_res.get("models_used") or [""])[0],
            "tokens_used": hdr_res.get("tokens_used", 0),
        }
        compact = _serialize_tool_response("update_header", tool_resp)
        db.resolve_gate(gate_id, "approved", compact)
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": compact}),
        )

    elif tool_name in ("patch_literal", "patch_lines", "write_file"):
        try:
            prepared = prepare_tool_gate_preview(project, tool_name, args)
            cp = (
                cp_svc.ensure_active_checkpoint(db, project, session.id)
                if hasattr(db, "get_active_checkpoint")
                else None
            )
            applied = patch_tools.apply_prepared_patch(
                project.root_path,
                project.backup_dir,
                prepared["prepared_patch"],
            )
        except Exception as exc:
            return _record_gate_error(exc)

        if cp is not None:
            cp_svc.record_write_in_checkpoint(db, cp, applied["path"], applied["existed_before"])

        patch_rec = db.add_patch_record(
            project_id=project.id,
            session_id=session.id,
            file_path=applied["path"],
            backup_path=applied["backup_path"],
            existed_before=applied["existed_before"],
            summary=gate["preview"].get("summary", tool_name),
            diff=applied["diff"],
        )
        tool_resp = {
            "applied": True,
            "path": applied["path"],
            "backup_path": applied["backup_path"],
            "bytes_written": applied["bytes_written"],
        }
        extra_ui["patch_record"] = patch_rec.model_dump()
        db.resolve_gate(gate_id, "approved", tool_resp)
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": tool_resp}),
        )

    elif tool_name == "delete_file":
        try:
            prepared = prepare_tool_gate_preview(project, tool_name, args)
            cp = (
                cp_svc.ensure_active_checkpoint(db, project, session.id)
                if hasattr(db, "get_active_checkpoint")
                else None
            )
            applied = patch_tools.apply_delete_file(
                project.root_path,
                project.backup_dir,
                prepared["prepared_patch"],
            )
        except Exception as exc:
            return _record_gate_error(exc)

        if cp is not None:
            cp_svc.record_write_in_checkpoint(db, cp, applied["path"], existed_before=True)

        patch_rec = db.add_patch_record(
            project_id=project.id,
            session_id=session.id,
            file_path=applied["path"],
            backup_path=applied["backup_path"],
            existed_before=True,
            summary=gate["preview"].get("summary", "Delete file"),
            diff=applied["diff"],
        )
        tool_resp = {
            "deleted": True,
            "path": applied["path"],
            "backup_path": applied["backup_path"],
        }
        extra_ui["patch_record"] = patch_rec.model_dump()
        db.resolve_gate(gate_id, "approved", tool_resp)
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": tool_resp}),
        )

    elif tool_name == "rename_file":
        try:
            prepared = prepare_tool_gate_preview(project, tool_name, args)
            cp = (
                cp_svc.ensure_active_checkpoint(db, project, session.id)
                if hasattr(db, "get_active_checkpoint")
                else None
            )
            applied = patch_tools.apply_rename_file(
                project.root_path,
                project.backup_dir,
                prepared["prepared_patch"],
            )
        except Exception as exc:
            return _record_gate_error(exc)

        if cp is not None:
            cp_svc.record_write_in_checkpoint(db, cp, applied["old_path"], existed_before=True)
            cp_svc.record_write_in_checkpoint(
                db, cp, applied["new_path"], existed_before=applied["dest_existed_before"]
            )

        patch_rec = db.add_patch_record(
            project_id=project.id,
            session_id=session.id,
            file_path=applied["old_path"],
            backup_path=applied["backup_path"],
            existed_before=True,
            summary=gate["preview"].get(
                "summary", f"Rename {applied['old_path']} -> {applied['new_path']}"
            ),
            diff=applied["diff"],
        )
        tool_resp = {
            "renamed": True,
            "old_path": applied["old_path"],
            "new_path": applied["new_path"],
            "backup_path": applied["backup_path"],
        }
        extra_ui["patch_record"] = patch_rec.model_dump()
        db.resolve_gate(gate_id, "approved", tool_resp)
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": tool_resp}),
        )

    elif tool_name == "propose_script":
        try:
            prepared = prepare_tool_gate_preview(project, tool_name, args)
            cp = (
                cp_svc.ensure_active_checkpoint(db, project, session.id)
                if hasattr(db, "get_active_checkpoint")
                else None
            )
            applied = patch_tools.apply_prepared_patch(
                project.root_path,
                project.backup_dir,
                prepared["prepared_patch"],
            )
        except Exception as exc:
            return _record_gate_error(exc)

        if cp is not None:
            cp_svc.record_write_in_checkpoint(db, cp, applied["path"], applied["existed_before"])

        patch_rec = db.add_patch_record(
            project_id=project.id,
            session_id=session.id,
            file_path=applied["path"],
            backup_path=applied["backup_path"],
            existed_before=applied["existed_before"],
            summary=gate["preview"].get("summary", "Script written"),
            diff=applied["diff"],
        )
        extra_ui["patch_record"] = patch_rec.model_dump()

        if decision.action in ("write_and_run", "write_run_and_delete"):
            auto_delete = decision.action == "write_run_and_delete"
            before_files = (
                cp_svc.list_project_relative_files(
                    project.root_path, project.backup_dir, project.ignore_patterns
                )
                if cp is not None
                else set()
            )
            try:
                run_res = await runner.stream_project_command(
                    app,
                    project.root_path,
                    f"python {shlex.quote(applied['path'])}",
                    configured_python=project.python_path,
                    source="gate",
                )
            except Exception as exc:
                if cp is not None:
                    after_files = cp_svc.list_project_relative_files(
                        project.root_path, project.backup_dir, project.ignore_patterns
                    )
                    for new_rel in sorted(after_files - before_files):
                        cp_svc.record_write_in_checkpoint(db, cp, new_rel, existed_before=False)
                err_resp = {
                    "written": True,
                    "executed": False,
                    "path": applied["path"],
                    "target": applied["path"],
                    "error": f"Failed to execute script '{applied['path']}': {exc}",
                }
                db.resolve_gate(gate_id, "error", err_resp)
                db.add_message(
                    session.id,
                    role="tool_event",
                    text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": err_resp}),
                )
                extra_ui["gate_error"] = err_resp["error"]
                return extra_ui

            if cp is not None:
                after_files = cp_svc.list_project_relative_files(
                    project.root_path, project.backup_dir, project.ignore_patterns
                )
                for new_rel in sorted(after_files - before_files):
                    cp_svc.record_write_in_checkpoint(db, cp, new_rel, existed_before=False)

            generated_md = bridge_svc.find_recent_markdown_outputs(
                project.root_path,
                run_res.get("output", ""),
                exclude_rel=applied["path"],
            )
            run_res["generated_md_files"] = generated_md
            extra_ui["command_result"] = run_res
            extra_ui["generated_md_files"] = generated_md
            raw_out = run_res.get("output") or ""
            exec_id = run_res.get("exec_id")
            focus_arg = str(args.get("focus") or "").strip() or None
            has_traceback = (
                "Traceback (most recent call last):" in raw_out
                or "\nSyntaxError:" in raw_out
                or "\nImportError:" in raw_out
                or "\nModuleNotFoundError:" in raw_out
            )
            passed = (run_res["exit_code"] == 0) and (not has_traceback)
            summary_info = await _maybe_summarize_or_triage_output(
                gemini=gemini_client,
                db=db,
                project=project,
                scout_cfg=scout_cfg,
                command=f"python {applied['path']}",
                raw_output=raw_out,
                exit_code=run_res["exit_code"],
                passed=passed,
                focus=focus_arg,
                exec_id=exec_id,
            )
            if summary_info.get("summary"):
                output_preview = ""
            elif not passed:
                output_preview = bridge_svc.extract_failure_excerpt(raw_out, max_chars=3500)
            else:
                output_preview = bridge_svc.format_command_output_preview(raw_out, max_chars=12000)

            deleted_after_run = False
            del_backup_path = None
            if auto_delete and passed:
                try:
                    del_preview = patch_tools.preview_delete_file(project.root_path, applied["path"])
                    del_res = patch_tools.apply_delete_file(
                        project.root_path, project.backup_dir, del_preview
                    )
                    deleted_after_run = bool(del_res.get("deleted"))
                    del_backup_path = del_res.get("backup_path")
                    del_rec = db.add_patch_record(
                        project_id=project.id,
                        session_id=session.id,
                        file_path=del_res["path"],
                        backup_path=del_backup_path,
                        existed_before=True,
                        summary=f"Auto-deleted temporary script after pass: {del_res['path']}",
                        diff=del_res["diff"],
                    )
                    extra_ui["patch_record"] = del_rec.model_dump()
                except Exception:
                    deleted_after_run = False

            tool_resp = {
                "written": True,
                "executed": True,
                "path": applied["path"],
                "exec_id": exec_id,
                "exit_code": run_res["exit_code"],
                "duration_ms": run_res["duration_ms"],
                "passed": passed,
                "deleted": deleted_after_run,
                "kept_for_patching": bool(auto_delete and not deleted_after_run),
                "backup_path": del_backup_path,
                "output_bytes": len(raw_out.encode("utf-8")),
                "output_preview": output_preview,
                "ai_summary": summary_info.get("summary"),
                "ai_summary_model": summary_info.get("model_used"),
                "has_traceback": has_traceback,
                "generated_md_files": generated_md if passed else [],
                "note": (
                    f"Auto-deleted '{applied['path']}' after clean run (backed up first). Raw output exec_id={exec_id}."
                    if deleted_after_run
                    else (
                        f"Script failed; kept '{applied['path']}' on disk so you can patch and re-run with run_and_delete_script."
                        if auto_delete and not passed
                        else f"Full output buffered as exec_id={exec_id} and shown in Console."
                    )
                ),
            }
            if not session.api_enabled and passed and generated_md:
                first_md_path = generated_md[0]["path"]
                try:
                    resolved_md = fs_tools.resolve_safe_path(project.root_path, first_md_path)
                    if resolved_md.is_file():
                        extra_ui["generated_md_path"] = first_md_path
                        extra_ui["generated_md_content"] = resolved_md.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        else:
            tool_resp = {
                "written": True,
                "executed": False,
                "path": applied["path"],
                "note": "Script written to disk; awaiting user execution.",
            }

        db.resolve_gate(gate_id, "approved", tool_resp)
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": tool_resp}),
        )

    elif tool_name in ("run_script", "run_and_delete_script"):
        auto_delete = tool_name == "run_and_delete_script"
        try:
            rel_path = str(args.get("path") or args.get("target") or "").strip()
            if not rel_path:
                raise ValueError(f"{tool_name} requires 'path'.")
            target_abs = fs_tools.resolve_safe_path(project.root_path, rel_path)
            if not target_abs.is_file():
                raise FileNotFoundError(f"Script not found on disk: {rel_path}")
            if target_abs.suffix.lower() != ".py":
                raise ValueError(f"{tool_name} only supports .py scripts inside project (got '{rel_path}').")
            rel_posix = target_abs.relative_to(Path(project.root_path).resolve()).as_posix()
            raw_args = args.get("args")
            if isinstance(raw_args, list):
                arg_list = [str(x) for x in raw_args if str(x) != ""]
            elif isinstance(raw_args, str) and raw_args.strip():
                arg_list = shlex.split(raw_args.strip(), posix=(os.name != "nt"))
            else:
                arg_list = []
            cmd = " ".join(["python", shlex.quote(rel_posix), *(shlex.quote(a) for a in arg_list)])
        except Exception as exc:
            return _record_gate_error(exc)

        cp = db.get_active_checkpoint(session.id) if hasattr(db, "get_active_checkpoint") else None
        before_files = (
            cp_svc.list_project_relative_files(
                project.root_path, project.backup_dir, project.ignore_patterns
            )
            if cp is not None
            else set()
        )
        try:
            run_res = await runner.stream_project_command(
                app,
                project.root_path,
                cmd,
                configured_python=project.python_path,
                source="gate",
            )
        except Exception as exc:
            if cp is not None:
                after_files = cp_svc.list_project_relative_files(
                    project.root_path, project.backup_dir, project.ignore_patterns
                )
                for new_rel in sorted(after_files - before_files):
                    cp_svc.record_write_in_checkpoint(db, cp, new_rel, existed_before=False)
            return _record_gate_error(exc)

        if cp is not None:
            after_files = cp_svc.list_project_relative_files(
                project.root_path, project.backup_dir, project.ignore_patterns
            )
            for new_rel in sorted(after_files - before_files):
                cp_svc.record_write_in_checkpoint(db, cp, new_rel, existed_before=False)

        generated_md = bridge_svc.find_recent_markdown_outputs(
            project.root_path,
            run_res.get("output", ""),
            exclude_rel=rel_posix,
        )
        run_res["generated_md_files"] = generated_md
        extra_ui["command_result"] = run_res
        extra_ui["generated_md_files"] = generated_md
        raw_out = run_res.get("output") or ""
        exec_id = run_res.get("exec_id")
        focus_arg = str(args.get("focus") or "").strip() or None
        has_traceback = (
            "Traceback (most recent call last):" in raw_out
            or "\nSyntaxError:" in raw_out
            or "\nImportError:" in raw_out
            or "\nModuleNotFoundError:" in raw_out
        )
        passed = (run_res["exit_code"] == 0) and (not has_traceback)
        summary_info = await _maybe_summarize_or_triage_output(
            gemini=gemini_client,
            db=db,
            project=project,
            scout_cfg=scout_cfg,
            command=cmd,
            raw_output=raw_out,
            exit_code=run_res["exit_code"],
            passed=passed,
            focus=focus_arg,
            exec_id=exec_id,
        )
        if summary_info.get("summary"):
            output_preview = ""
        elif not passed:
            output_preview = bridge_svc.extract_failure_excerpt(raw_out, max_chars=3500)
        else:
            output_preview = bridge_svc.format_command_output_preview(raw_out, max_chars=12000)

        deleted_after_run = False
        del_backup_path = None
        if auto_delete and passed and target_abs.is_file():
            try:
                del_preview = patch_tools.preview_delete_file(project.root_path, rel_posix)
                del_res = patch_tools.apply_delete_file(
                    project.root_path, project.backup_dir, del_preview
                )
                deleted_after_run = bool(del_res.get("deleted"))
                del_backup_path = del_res.get("backup_path")
                if cp is not None:
                    cp_svc.record_write_in_checkpoint(db, cp, rel_posix, existed_before=True)
                del_rec = db.add_patch_record(
                    project_id=project.id,
                    session_id=session.id,
                    file_path=del_res["path"],
                    backup_path=del_backup_path,
                    existed_before=True,
                    summary=f"Auto-deleted temporary script after pass: {del_res['path']}",
                    diff=del_res["diff"],
                )
                extra_ui["patch_record"] = del_rec.model_dump()
            except Exception:
                deleted_after_run = False

        tool_resp = {
            "executed": True,
            "path": rel_posix,
            "command": cmd,
            "exec_id": exec_id,
            "exit_code": run_res["exit_code"],
            "duration_ms": run_res["duration_ms"],
            "passed": passed,
            "has_traceback": has_traceback,
            "deleted": deleted_after_run,
            "kept_for_patching": bool(auto_delete and not deleted_after_run),
            "backup_path": del_backup_path,
            "output_bytes": len(raw_out.encode("utf-8")),
            "output_preview": output_preview,
            "ai_summary": summary_info.get("summary"),
            "ai_summary_model": summary_info.get("model_used"),
            "generated_md_files": generated_md if passed else [],
            "note": (
                f"Auto-deleted '{rel_posix}' after clean run (backed up first). Raw output exec_id={exec_id}."
                if deleted_after_run
                else (
                    f"Script failed; kept '{rel_posix}' on disk so you can patch it and re-run run_and_delete_script."
                    if auto_delete and not passed
                    else f"Full output buffered as exec_id={exec_id} and shown in Console."
                )
            ),
        }
        if not session.api_enabled and passed and generated_md:
            first_md_path = generated_md[0]["path"]
            try:
                resolved_md = fs_tools.resolve_safe_path(project.root_path, first_md_path)
                if resolved_md.is_file():
                    extra_ui["generated_md_path"] = first_md_path
                    extra_ui["generated_md_content"] = resolved_md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

        db.resolve_gate(gate_id, "approved", tool_resp)
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": tool_resp}),
        )

    elif tool_name == "run_tests":
        try:
            fast = bool(args.get("fast", False))
            target = str(args.get("target") or "").strip() or None
            resolved = runner.resolve_test_command(project, fast=fast, target=target)
            cmd = resolved["command"]
            is_full_suite = bool(resolved["is_full_suite"])
        except Exception as exc:
            return _record_gate_error(exc)

        try:
            run_res = await runner.stream_project_command(
                app,
                project.root_path,
                cmd,
                configured_python=project.python_path,
                source="gate",
            )
        except Exception as exc:
            return _record_gate_error(exc)

        extra_ui["command_result"] = run_res
        raw_out = run_res.get("output") or ""
        exec_id = run_res.get("exec_id")
        focus_arg = str(args.get("focus") or "").strip() or None
        has_traceback = (
            "Traceback (most recent call last):" in raw_out
            or "\nSyntaxError:" in raw_out
            or "\nAssertionError:" in raw_out
            or "\nImportError:" in raw_out
            or "\nModuleNotFoundError:" in raw_out
        )
        passed = (run_res["exit_code"] == 0) and (not has_traceback)
        summary_info = await _maybe_summarize_or_triage_output(
            gemini=gemini_client,
            db=db,
            project=project,
            scout_cfg=scout_cfg,
            command=cmd,
            raw_output=raw_out,
            exit_code=run_res["exit_code"],
            passed=passed,
            focus=focus_arg,
            exec_id=exec_id,
        )
        committed_cp = None
        if summary_info.get("summary"):
            output_preview = ""
        elif passed:
            output_preview = bridge_svc.format_command_output_preview(raw_out, max_chars=2500)
        else:
            output_preview = bridge_svc.extract_failure_excerpt(raw_out, max_chars=3500)
        if passed and is_full_suite and hasattr(db, "get_active_checkpoint"):
            committed_cp = cp_svc.auto_commit_active_checkpoint_on_tests_pass(
                db, project, session.id, cmd
            )

        if committed_cp is not None:
            extra_ui["checkpoint_committed"] = True

        tool_resp = {
            "command": cmd,
            "exec_id": exec_id,
            "is_full_suite": is_full_suite,
            "mode": resolved["mode"],
            "target": resolved["target"],
            "exit_code": run_res["exit_code"],
            "duration_ms": run_res["duration_ms"],
            "passed": passed,
            "has_traceback": has_traceback,
            "output_bytes": len(raw_out.encode("utf-8")),
            "output_preview": output_preview,
            "ai_summary": summary_info.get("summary"),
            "ai_summary_model": summary_info.get("model_used"),
            "checkpoint_committed": bool(committed_cp is not None),
        }
        db.resolve_gate(gate_id, "approved", tool_resp)
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "call_id": call_id, "response": tool_resp}),
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unhandled gate tool: {tool_name}")

    return extra_ui


@router.post("/gate/{gate_id}")
async def resolve_tool_gate(
    gate_id: str,
    decision: ToolGateDecision,
    request: Request,
) -> Dict[str, Any]:
    db = request.app.state.db
    gemini = request.app.state.gemini

    gate = db.get_gate(gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail="Tool gate not found.")
    if gate["status"] != "pending":
        raise HTTPException(status_code=400, detail="Tool gate already resolved.")

    session = db.get_session(gate["session_id"])
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    project = db.get_project(session.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    app = getattr(request, "app", None)
    if app is not None and hasattr(app, "state"):
        app.state.event_loop = asyncio.get_running_loop()
    extra_ui = await _execute_gate_decision(
        db, gate, decision, session, project, app=app, gemini=gemini
    )
    if extra_ui.get("gate_error"):
        extra_ui["gate_errors"] = [extra_ui["gate_error"]]
    if extra_ui.get("command_result"):
        extra_ui["command_results"] = [extra_ui["command_result"]]

    messages = db.list_messages(session.id)
    if extra_ui.get("gate_errors"):
        extra_ui["error_reply_text"] = bridge_svc.format_trailing_tool_events(
            messages, errors_only=True
        )

    checkpoints = db.list_checkpoints(session.id) if hasattr(db, "list_checkpoints") else []
    remaining = db.list_pending_gates(session.id)
    if remaining:
        return {
            "session": db.get_session(session.id),
            "messages": messages,
            "pending_gates": remaining,
            "checkpoints": checkpoints,
            "telemetry": local_scout.get_telemetry_snapshot(
                db, gemini, model=getattr(session, "model", None)
            ),
            **extra_ui,
        }

    if not session.api_enabled:
        scout_cfg = _get_effective_scout_settings(db, gemini)
        reply_text = _maybe_append_scout_notice(
            db,
            db.get_session(session.id) or session,
            bool(scout_cfg.get("enabled")),
            bridge_svc.format_trailing_tool_events(messages),
        )
        return {
            "session": db.get_session(session.id),
            "messages": messages,
            "pending_gates": [],
            "checkpoints": checkpoints,
            "bridge_reply_text": reply_text,
            "telemetry": local_scout.get_telemetry_snapshot(
                db, gemini, model=getattr(session, "model", None)
            ),
            **extra_ui,
        }

    try:
        result = _advance_turn(db, gemini, session, project, app=app)
        result.update(extra_ui)
        return result
    except Exception as exc:
        raise _build_gemini_http_exception(gemini, session.model, exc) from exc


@router.post("/gates/batch")
async def resolve_tool_gates_batch(
    payload: BatchGateDecisionRequest,
    request: Request,
) -> Dict[str, Any]:
    db = request.app.state.db
    gemini = request.app.state.gemini

    session = db.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    project = db.get_project(session.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    app = getattr(request, "app", None)
    if app is not None and hasattr(app, "state"):
        app.state.event_loop = asyncio.get_running_loop()
    last_extra: Dict[str, Any] = {}
    gate_errors: List[str] = []
    command_results: List[Dict[str, Any]] = []
    for item in payload.decisions:
        gate = db.get_gate(item.gate_id)
        if gate is None or gate["session_id"] != session.id or gate["status"] != "pending":
            continue
        extra_ui = await _execute_gate_decision(
            db, gate, item.decision, session, project, app=app, gemini=gemini
        )
        if extra_ui.get("gate_error"):
            gate_errors.append(extra_ui["gate_error"])
        if extra_ui.get("command_result"):
            command_results.append(extra_ui["command_result"])
        last_extra.update(extra_ui)

    if command_results:
        last_extra["command_results"] = command_results
    messages = db.list_messages(session.id)
    if gate_errors:
        last_extra["gate_errors"] = gate_errors
        last_extra["error_reply_text"] = bridge_svc.format_trailing_tool_events(
            messages, errors_only=True
        )

    checkpoints = db.list_checkpoints(session.id) if hasattr(db, "list_checkpoints") else []
    remaining = db.list_pending_gates(session.id)
    if remaining:
        return {
            "session": db.get_session(session.id),
            "messages": messages,
            "pending_gates": remaining,
            "checkpoints": checkpoints,
            "telemetry": local_scout.get_telemetry_snapshot(
                db, gemini, model=getattr(session, "model", None)
            ),
            **last_extra,
        }

    if not session.api_enabled:
        scout_cfg = _get_effective_scout_settings(db, gemini)
        reply_text = _maybe_append_scout_notice(
            db,
            db.get_session(session.id) or session,
            bool(scout_cfg.get("enabled")),
            bridge_svc.format_trailing_tool_events(messages),
        )
        return {
            "session": db.get_session(session.id),
            "messages": messages,
            "pending_gates": [],
            "checkpoints": checkpoints,
            "bridge_reply_text": reply_text,
            "telemetry": local_scout.get_telemetry_snapshot(
                db, gemini, model=getattr(session, "model", None)
            ),
            **last_extra,
        }

    try:
        result = _advance_turn(db, gemini, session, project, app=app)
        result.update(last_extra)
        return result
    except Exception as exc:
        raise _build_gemini_http_exception(gemini, session.model, exc) from exc
