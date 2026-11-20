"""
routes/bridge.py - Playground Bridge endpoints (0 Gemini API tokens spent).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request

from models import BridgeBundleRequest, BridgePackFilesRequest, BridgeParseRequest
from services import bridge as bridge_svc
from services import fs_tools
from services.gemini_client import READ_ONLY_TOOLS, prepare_tool_gate_preview
from services.token_utils import estimate_tokens

router = APIRouter(prefix="/api/bridge", tags=["bridge"])


@router.post("/bundle")
def create_bridge_bundle(payload: BridgeBundleRequest, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    session = db.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.api_enabled:
        raise HTTPException(status_code=403, detail="Bridge bundling is disabled inside API sessions.")
    project = db.get_project(session.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    include_dir = (
        bool(payload.include_directives)
        if payload.include_directives is not None
        else (not session.directives_sent)
    )
    bundle = bridge_svc.build_prompt_bundle(
        project=project,
        prompt=payload.prompt,
        attachments=payload.attachments,
        include_directives=include_dir,
    )

    if payload.record_turn and (payload.prompt.strip() or payload.attachments or include_dir):
        if payload.prompt.strip():
            user_text = payload.prompt.strip()
        elif payload.attachments and include_dir:
            user_text = "Sent Project Directives + Bridge Tool Contract with attachments"
        elif include_dir:
            user_text = "Sent Project Directives + Bridge Tool Contract"
        else:
            user_text = "(Bridge bundle copied with attachments)"
        db.add_message(
            session.id,
            role="user",
            text=user_text,
            attachments=payload.attachments,
        )

    if include_dir and not session.directives_sent:
        from models import SessionUpdate

        db.update_session(session.id, SessionUpdate(directives_sent=True))

    return {
        **bundle,
        "session": db.get_session(session.id),
        "messages": db.list_messages(session.id),
    }


@router.post("/parse")
def parse_bridge_response(payload: BridgeParseRequest, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    session = db.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.api_enabled:
        raise HTTPException(status_code=403, detail="Bridge parsing is disabled inside API sessions.")
    project = db.get_project(session.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    parsed = bridge_svc.parse_playground_markdown(payload.raw_markdown)
    actions = parsed["actions"]
    prose = parsed["prose_text"]

    tool_calls_trace = [
        {"name": a["tool"], "args": a["args"]}
        for a in actions
    ]
    db.add_message(
        session.id,
        role="model",
        text=prose,
        tool_calls=tool_calls_trace,
    )

    staged = 0
    auto_executed = 0
    error_count = 0
    warnings: List[str] = []
    if not actions and re.search(r'"(?:tool|tool_name)"\s*:', payload.raw_markdown or ""):
        warnings.append(
            "Detected tool JSON markers in pasted text, but could not extract valid tool actions "
            "(the output may be truncated or missing closing braces/brackets)."
        )
    for idx, act in enumerate(actions):
        tool_name = act["tool"]
        args = act["args"]
        try:
            preview = prepare_tool_gate_preview(project, tool_name, args)
        except Exception as exc:
            err_msg = f"{tool_name}: {exc}"
            warnings.append(err_msg)
            error_count += 1
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
                    "tool_name": tool_name,
                    "call_id": f"bridge-{idx}",
                    "response": {"error": str(exc), "target": target_label},
                }),
            )
            continue

        # Safety guarantee: never auto-approve writes, patches, or executable scripts
        if (
            payload.auto_approve_reads
            and tool_name in READ_ONLY_TOOLS
            and preview.get("category") == "read"
        ):
            compact = bridge_svc.serialize_tool_response(tool_name, preview["prepared_result"])
            db.add_message(
                session.id,
                role="tool_event",
                text=json.dumps({
                    "tool_name": tool_name,
                    "call_id": f"bridge-{idx}",
                    "response": compact,
                }),
            )
            auto_executed += 1
            continue

        ui_preview = {
            k: v for k, v in preview.items()
            if k not in ("prepared_result", "prepared_patch")
        }
        ui_preview["call_id"] = f"bridge-{idx}"
        db.create_gate(
            session_id=session.id,
            tool_name=tool_name,
            args=args,
            preview=ui_preview,
        )
        staged += 1

    pending_gates = db.list_pending_gates(session.id)
    messages = db.list_messages(session.id)
    bridge_reply_text = ""
    if (auto_executed > 0 or error_count > 0) and not pending_gates:
        bridge_reply_text = bridge_svc.format_trailing_tool_events(messages)
    error_reply_text = (
        bridge_svc.format_trailing_tool_events(messages, errors_only=True)
        if error_count > 0
        else ""
    )

    return {
        "session": db.get_session(session.id),
        "messages": messages,
        "pending_gates": pending_gates,
        "checkpoints": db.list_checkpoints(session.id) if hasattr(db, "list_checkpoints") else [],
        "parsed_count": len(actions),
        "staged_count": staged,
        "auto_executed_count": auto_executed,
        "error_count": error_count,
        "bridge_reply_text": bridge_reply_text,
        "error_reply_text": error_reply_text,
        "warnings": warnings,
    }


@router.get("/read-md")
def read_generated_markdown(
    request: Request,
    project_id: str = Query(...),
    path: str = Query(...),
    session_id: str | None = Query(default=None),
    record_summary: bool = Query(default=False),
) -> Dict[str, Any]:
    db = request.app.state.db
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        fpath = fs_tools.resolve_safe_path(project.root_path, path)
        if not fpath.is_file():
            raise FileNotFoundError(f"Markdown file not found: {path}")
        content = fpath.read_text(encoding="utf-8", errors="replace")
        size_bytes = len(content.encode("utf-8"))
        est_tok = estimate_tokens(content)
        messages = None
        if record_summary and session_id:
            session = db.get_session(session_id)
            if session is not None and not session.api_enabled:
                size_label = f"{size_bytes / 1024:.1f} KB" if size_bytes >= 1024 else f"{size_bytes} B"
                summary_line = f"Requested content generated: {path} ({size_label} · ~{est_tok:,} tok)"
                existing = db.list_messages(session_id)
                if not existing or existing[-1].text != summary_line:
                    db.add_message(session_id, role="user", text=summary_line)
                messages = db.list_messages(session_id)
        out: Dict[str, Any] = {
            "path": path,
            "content": content,
            "size_bytes": size_bytes,
            "estimated_tokens": est_tok,
        }
        if messages is not None:
            out["messages"] = messages
        return out
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pack-files")
def pack_filtered_files(payload: BridgePackFilesRequest, request: Request) -> Dict[str, Any]:
    project = request.app.state.db.get_project(payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        return bridge_svc.pack_project_files(
            project=project,
            paths=payload.paths or None,
            filter_text=payload.filter_text,
            max_files=max(1, min(60, payload.max_files)),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
