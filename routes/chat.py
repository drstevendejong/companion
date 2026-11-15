"""
routes/chat.py - Human-gated Gemini chat loop and tool approval/denial/edit-range endpoints.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from google.genai import types

from models import ChatSendRequest, MessageRecord, ProjectProfile, SessionInfo, ToolGateDecision
from services import fs_tools, patch_tools, runner
from services.gemini_client import (
    READ_ONLY_TOOLS,
    build_user_parts,
    prepare_tool_gate_preview,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _messages_to_history(messages: List[MessageRecord]) -> List[types.Content]:
    """Reconstructs valid multi-turn Gemini Contents including function calls & responses."""
    contents: List[types.Content] = []
    pending_tool_parts: List[types.Part] = []

    def _flush_tools() -> None:
        if pending_tool_parts:
            contents.append(types.Content(role="user", parts=list(pending_tool_parts)))
            pending_tool_parts.clear()

    for msg in messages:
        if msg.role == "user":
            _flush_tools()
            parts = build_user_parts(msg.text, msg.attachments)
            contents.append(types.Content(role="user", parts=parts))
        elif msg.role == "model":
            _flush_tools()
            parts: List[types.Part] = []
            if msg.text:
                parts.append(types.Part.from_text(text=msg.text))
            for tc in msg.tool_calls:
                parts.append(
                    types.Part.from_function_call(
                        name=tc["name"],
                        args=tc.get("args", {}),
                    )
                )
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        elif msg.role == "tool_event":
            payload = json.loads(msg.text)
            pending_tool_parts.append(
                types.Part.from_function_response(
                    name=payload["tool_name"],
                    response=payload["response"],
                )
            )
    _flush_tools()
    return contents


def _serialize_tool_response(tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Shapes compact tool responses returned to Gemini to conserve tokens."""
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


def _advance_turn(
    db: Any,
    gemini: Any,
    session: SessionInfo,
    project: ProjectProfile,
    max_auto_loops: int = 5,
) -> Dict[str, Any]:
    """Runs Gemini turn(s), auto-executing only approved-threshold reads, pausing at gates."""
    auto_loops = 0
    while True:
        history = _messages_to_history(db.list_messages(session.id))
        turn = gemini.generate_turn(session, project, history)
        usage = turn["usage"]
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
            if not is_auto_read:
                all_auto_approved = False

        if all_auto_approved and auto_loops < max_auto_loops:
            auto_loops += 1
            for tc, preview in prepared_items:
                compact = _serialize_tool_response(tc["name"], preview["prepared_result"])
                db.add_message(
                    session.id,
                    role="tool_event",
                    text=json.dumps({"tool_name": tc["name"], "response": compact}),
                )
            session = updated_session
            continue

        for tc, preview in prepared_items:
            if preview.get("category") == "error":
                db.add_message(
                    session.id,
                    role="tool_event",
                    text=json.dumps({
                        "tool_name": tc["name"],
                        "response": {"error": preview["error"]},
                    }),
                )
                continue

            ui_preview = {
                k: v for k, v in preview.items()
                if k not in ("prepared_result", "prepared_patch")
            }
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
    }


@router.post("/send")
def send_chat_message(payload: ChatSendRequest, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    gemini = request.app.state.gemini

    session = db.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
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
        return _advance_turn(db, gemini, session, project)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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

    tool_name = gate["tool_name"]
    args = gate["args"]
    extra_ui: Dict[str, Any] = {}

    if decision.action == "deny":
        tool_resp = {
            "denied_by_user": True,
            "reason": decision.reason or "User declined this action. Discuss or propose another approach.",
        }
        db.resolve_gate(gate_id, "denied", tool_resp)
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "response": tool_resp}),
        )

    elif tool_name in READ_ONLY_TOOLS:
        if decision.action == "edit_range" and tool_name in ("read_lines", "read_file"):
            s = decision.start_line or 1
            e = decision.end_line or (s + 150)
            res = fs_tools.read_lines(project.root_path, str(args["path"]), s, e)
            compact = _serialize_tool_response("read_lines", res)
        else:
            prepared = prepare_tool_gate_preview(project, tool_name, args)
            compact = _serialize_tool_response(tool_name, prepared["prepared_result"])

        db.resolve_gate(gate_id, "approved", {"bytes": len(json.dumps(compact))})
        db.add_message(
            session.id,
            role="tool_event",
            text=json.dumps({"tool_name": tool_name, "response": compact}),
        )

    elif tool_name in ("patch_literal", "patch_lines", "write_file"):
        prepared = prepare_tool_gate_preview(project, tool_name, args)
        applied = patch_tools.apply_prepared_patch(
            project.root_path,
            project.backup_dir,
            prepared["prepared_patch"],
        )
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
            text=json.dumps({"tool_name": tool_name, "response": tool_resp}),
        )

    elif tool_name == "propose_script":
        prepared = prepare_tool_gate_preview(project, tool_name, args)
        applied = patch_tools.apply_prepared_patch(
            project.root_path,
            project.backup_dir,
            prepared["prepared_patch"],
        )
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

        if decision.action == "write_and_run":
            run_res = await runner.run_project_command(
                project.root_path,
                f"python {applied['path']}",
                configured_python=project.python_path,
            )
            extra_ui["command_result"] = run_res
            tool_resp = {
                "written": True,
                "executed": True,
                "path": applied["path"],
                "exit_code": run_res["exit_code"],
                "duration_ms": run_res["duration_ms"],
                "note": "Full output captured in user's local Terminal panel. User may paste relevant sections.",
            }
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
            text=json.dumps({"tool_name": tool_name, "response": tool_resp}),
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unhandled gate tool: {tool_name}")

    remaining = db.list_pending_gates(session.id)
    if remaining:
        return {
            "session": db.get_session(session.id),
            "messages": db.list_messages(session.id),
            "pending_gates": remaining,
            **extra_ui,
        }

    try:
        result = _advance_turn(db, gemini, session, project)
        result.update(extra_ui)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
