"""
routes/bridge.py - Playground Bridge endpoints (0 Gemini API tokens spent).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from models import (
    BridgeBundleRequest,
    BridgePackFilesRequest,
    BridgeParseRequest,
    SessionUpdate,
)
from routes.chat import (
    _get_effective_scout_settings,
    _maybe_append_scout_notice,
    execute_approved_script_patch,
    execute_existing_script_run,
)
from services import bridge as bridge_svc
from services import fs_tools, local_scout
from services.gemini_client import READ_ONLY_TOOLS, SCOUT_TOOLS, prepare_tool_gate_preview
from services.token_utils import estimate_tokens

router = APIRouter(prefix="/api/bridge", tags=["bridge"])


@router.post("/bundle")
def create_bridge_bundle(payload: BridgeBundleRequest, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    gemini = getattr(request.app.state, "gemini", None)
    session = db.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.api_enabled:
        raise HTTPException(status_code=403, detail="Bridge bundling is disabled inside API sessions.")
    project = db.get_project(session.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    scout_cfg = _get_effective_scout_settings(db, gemini)
    scout_enabled = bool(scout_cfg.get("enabled"))
    target_bit = 1 if scout_enabled else 0
    current_announced = getattr(session, "scout_announced_state", 0)
    scout_notice = ""
    if current_announced != target_bit:
        scout_notice = bridge_svc.build_scout_state_notice(scout_enabled)

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
        scout_enabled=scout_enabled,
        scout_state_notice=scout_notice,
        db=db,
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

    updates: Dict[str, Any] = {}
    if include_dir and not session.directives_sent:
        updates["directives_sent"] = True
    if payload.record_turn and current_announced != target_bit:
        updates["scout_announced_state"] = target_bit
    if updates:
        db.update_session(session.id, SessionUpdate(**updates))

    return {
        **bundle,
        "session": db.get_session(session.id),
        "messages": db.list_messages(session.id),
    }


@router.post("/parse")
async def parse_bridge_response(payload: BridgeParseRequest, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    gemini = getattr(request.app.state, "gemini", None)
    app = getattr(request, "app", None)
    if app is not None and hasattr(app, "state"):
        app.state.event_loop = asyncio.get_running_loop()

    session = db.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.api_enabled:
        raise HTTPException(status_code=403, detail="Bridge parsing is disabled inside API sessions.")
    project = db.get_project(session.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    scout_cfg = _get_effective_scout_settings(db, gemini)
    scout_enabled = bool(scout_cfg.get("enabled"))
    scout_auto = bool(scout_cfg.get("auto_approve"))

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

    def _parse_script_args(raw_args: Any) -> tuple[List[str], Optional[str]]:
        if isinstance(raw_args, list):
            lst = [str(x) for x in raw_args if str(x) != ""]
            return lst, (" ".join(lst) if lst else None)
        if isinstance(raw_args, str) and raw_args.strip():
            txt = raw_args.strip()
            return shlex.split(txt, posix=(os.name != "nt")), txt
        return [], None

    approved_scripts = bridge_svc.get_approved_inspection_scripts(
        db.list_messages(session.id), project.root_path
    )
    last_patch_idx_by_path: Dict[str, int] = {}
    last_run_idx_by_path: Dict[str, int] = {}
    for idx, act in enumerate(actions):
        tname = act["tool"]
        targs = act["args"]
        traw = str(targs.get("path") or targs.get("target") or "").strip()
        trel = bridge_svc.normalize_project_rel_path(project.root_path, traw) if traw else None
        if not (trel and trel in approved_scripts):
            continue
        if tname in (
            "patch_literal",
            "patch_lines",
            "write_file",
            "propose_inspection_script",
            "propose_script",
        ) or (
            tname == "run_and_delete_script"
            and isinstance(targs.get("content"), str)
            and targs["content"].strip()
        ):
            last_patch_idx_by_path[trel] = idx
        elif tname in ("run_and_delete_script", "run_script"):
            last_run_idx_by_path[trel] = idx

    staged = 0
    auto_executed = 0
    error_count = 0
    warnings: List[str] = []
    command_results: List[Dict[str, Any]] = []
    last_patch_record: Optional[Dict[str, Any]] = None
    generated_md_files: List[Dict[str, Any]] = []
    generated_md_path: Optional[str] = None
    generated_md_content: Optional[str] = None

    def _collect_run_ui(run_ui: Dict[str, Any]) -> None:
        nonlocal last_patch_record, generated_md_path, generated_md_content
        if run_ui.get("command_result"):
            command_results.append(run_ui["command_result"])
        if run_ui.get("patch_record"):
            last_patch_record = run_ui["patch_record"]
        for md_item in run_ui.get("generated_md_files") or []:
            if md_item and md_item.get("path") and not any(
                x.get("path") == md_item["path"] for x in generated_md_files
            ):
                generated_md_files.append(md_item)
        if run_ui.get("generated_md_path") and run_ui.get("generated_md_content") is not None:
            generated_md_path = run_ui["generated_md_path"]
            generated_md_content = run_ui["generated_md_content"]

    if not actions and re.search(r'"(?:tool|tool_name)"\s*:', payload.raw_markdown or ""):
        warnings.append(
            "Detected tool JSON markers in pasted text, but could not extract valid tool actions "
            "(the output may be truncated or missing closing braces/brackets)."
        )
    for idx, act in enumerate(actions):
        tool_name = act["tool"]
        args = act["args"]
        target_label = str(
            args.get("path")
            or args.get("old_path")
            or args.get("target")
            or args.get("pattern")
            or args.get("subpath")
            or ""
        )
        if tool_name in SCOUT_TOOLS and not scout_enabled:
            err_msg = f"{tool_name}: Local AI Scout is currently OFF or missing a configured API key."
            warnings.append(err_msg)
            error_count += 1
            db.add_message(
                session.id,
                role="tool_event",
                text=json.dumps({
                    "tool_name": tool_name,
                    "call_id": f"bridge-{idx}",
                    "response": {
                        "error": "Local AI Scout is currently OFF or missing a configured API key.",
                        "target": target_label,
                    },
                }),
            )
            continue

        try:
            preview = prepare_tool_gate_preview(project, tool_name, args)
        except Exception as exc:
            err_msg = f"{tool_name}: {exc}"
            warnings.append(err_msg)
            error_count += 1
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

        # Safety guarantee: never auto-approve arbitrary writes, patches, or unapproved scripts
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

        if (
            tool_name == "scout_workspace"
            and scout_enabled
            and scout_auto
            and preview.get("category") == "scout"
            and gemini is not None
        ):
            try:
                scout_res = await asyncio.to_thread(
                    local_scout.run_scout_workspace,
                    gemini,
                    db,
                    project,
                    session_id=session.id,
                    goal=str(preview.get("goal") or ""),
                    subpath=str(preview.get("subpath") or "."),
                    max_steps=int(preview.get("max_steps") or 8),
                    max_tokens=int(scout_cfg.get("max_tokens") or 30_000),
                    app=app,
                    event_loop=asyncio.get_running_loop(),
                )
                compact = bridge_svc.serialize_tool_response("scout_workspace", scout_res)
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
            except Exception as exc:
                warnings.append(f"scout_workspace: {exc}")
                error_count += 1
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

        # Auto-approve patches & re-runs for already-approved active inspection scripts
        target_raw = str(args.get("path") or args.get("target") or "").strip()
        target_rel = (
            bridge_svc.normalize_project_rel_path(project.root_path, target_raw)
            if target_raw
            else None
        )
        if target_rel and target_rel in approved_scripts:
            script_meta = approved_scripts[target_rel]
            is_script_patch = tool_name in ("patch_literal", "patch_lines", "write_file")
            is_script_repropose = tool_name in ("propose_inspection_script", "propose_script") or (
                tool_name == "run_and_delete_script" and "prepared_patch" in preview
            )
            is_script_rerun = tool_name in ("run_and_delete_script", "run_script") and not is_script_repropose

            if is_script_patch:
                try:
                    patch_resp, patch_extra = await execute_approved_script_patch(
                        db,
                        session,
                        project,
                        tool_name,
                        args,
                        preview,
                        summary_fallback=str(preview.get("summary") or tool_name),
                    )
                    if patch_extra.get("patch_record"):
                        last_patch_record = patch_extra["patch_record"]
                    db.add_message(
                        session.id,
                        role="tool_event",
                        text=json.dumps({
                            "tool_name": tool_name,
                            "call_id": f"bridge-{idx}",
                            "response": patch_resp,
                        }),
                    )
                    auto_executed += 1
                except Exception as exc:
                    warnings.append(f"{tool_name}: {exc}")
                    error_count += 1
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

                if (
                    idx == last_patch_idx_by_path.get(target_rel)
                    and last_run_idx_by_path.get(target_rel, -1) <= idx
                ):
                    auto_del = bool(script_meta.get("auto_delete", True))
                    rerun_tool = "run_and_delete_script" if auto_del else "run_script"
                    raw_args = args.get("args") if args.get("args") is not None else script_meta.get("args")
                    arg_list, raw_args_txt = _parse_script_args(raw_args)
                    try:
                        run_resp, run_ui = await execute_existing_script_run(
                            db,
                            session,
                            project,
                            rel_posix=target_rel,
                            auto_delete=auto_del,
                            arg_list=arg_list,
                            raw_args_text=raw_args_txt,
                            focus=str(args.get("focus") or "").strip() or None,
                            app=app,
                            gemini_client=gemini,
                            scout_cfg=scout_cfg,
                            auto_approved_inspection=True,
                            auto_rerun_after_patch=True,
                        )
                        _collect_run_ui(run_ui)
                        if run_resp.get("deleted"):
                            approved_scripts.pop(target_rel, None)
                        db.add_message(
                            session.id,
                            role="tool_event",
                            text=json.dumps({
                                "tool_name": rerun_tool,
                                "call_id": f"bridge-{idx}-rerun",
                                "response": run_resp,
                            }),
                        )
                    except Exception as exc:
                        warnings.append(f"{rerun_tool}: {exc}")
                        error_count += 1
                        db.add_message(
                            session.id,
                            role="tool_event",
                            text=json.dumps({
                                "tool_name": rerun_tool,
                                "call_id": f"bridge-{idx}-rerun",
                                "response": {"error": str(exc), "target": target_rel},
                            }),
                        )
                continue

            if is_script_repropose:
                try:
                    patch_resp, patch_extra = await execute_approved_script_patch(
                        db,
                        session,
                        project,
                        tool_name,
                        args,
                        preview,
                        summary_fallback=str(preview.get("summary") or tool_name),
                    )
                    if patch_extra.get("patch_record"):
                        last_patch_record = patch_extra["patch_record"]
                except Exception as exc:
                    warnings.append(f"{tool_name}: {exc}")
                    error_count += 1
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

                if last_run_idx_by_path.get(target_rel, -1) <= idx:
                    auto_del = bool(args.get("delete_on_success", script_meta.get("auto_delete", True)))
                    raw_args = args.get("args") if args.get("args") is not None else script_meta.get("args")
                    arg_list, raw_args_txt = _parse_script_args(raw_args)
                    try:
                        run_resp, run_ui = await execute_existing_script_run(
                            db,
                            session,
                            project,
                            rel_posix=target_rel,
                            auto_delete=auto_del,
                            arg_list=arg_list,
                            raw_args_text=raw_args_txt,
                            focus=str(args.get("focus") or "").strip() or None,
                            app=app,
                            gemini_client=gemini,
                            scout_cfg=scout_cfg,
                            auto_approved_inspection=True,
                            auto_rerun_after_patch=True,
                        )
                        _collect_run_ui(run_ui)
                        if run_resp.get("deleted"):
                            approved_scripts.pop(target_rel, None)
                        merged_resp = {
                            **run_resp,
                            "written": True,
                            "backup_path": run_resp.get("backup_path") or patch_resp.get("backup_path"),
                            "bytes_written": patch_resp.get("bytes_written"),
                        }
                        db.add_message(
                            session.id,
                            role="tool_event",
                            text=json.dumps({
                                "tool_name": tool_name,
                                "call_id": f"bridge-{idx}",
                                "response": merged_resp,
                            }),
                        )
                        auto_executed += 1
                    except Exception as exc:
                        warnings.append(f"{tool_name}: {exc}")
                        error_count += 1
                        db.add_message(
                            session.id,
                            role="tool_event",
                            text=json.dumps({
                                "tool_name": tool_name,
                                "call_id": f"bridge-{idx}",
                                "response": {"error": str(exc), "target": target_rel},
                            }),
                        )
                else:
                    db.add_message(
                        session.id,
                        role="tool_event",
                        text=json.dumps({
                            "tool_name": tool_name,
                            "call_id": f"bridge-{idx}",
                            "response": {
                                "written": True,
                                "executed": False,
                                "path": patch_resp["path"],
                                "backup_path": patch_resp.get("backup_path"),
                                "bytes_written": patch_resp.get("bytes_written"),
                                "auto_approved_inspection_script": True,
                            },
                        }),
                    )
                    auto_executed += 1
                continue

            if is_script_rerun:
                auto_del = (
                    tool_name == "run_and_delete_script"
                    or bool(script_meta.get("auto_delete"))
                )
                raw_args = args.get("args") if args.get("args") is not None else script_meta.get("args")
                arg_list, raw_args_txt = _parse_script_args(raw_args)
                try:
                    run_resp, run_ui = await execute_existing_script_run(
                        db,
                        session,
                        project,
                        rel_posix=target_rel,
                        auto_delete=auto_del,
                        arg_list=arg_list,
                        raw_args_text=raw_args_txt,
                        focus=str(args.get("focus") or "").strip() or None,
                        app=app,
                        gemini_client=gemini,
                        scout_cfg=scout_cfg,
                        auto_approved_inspection=True,
                    )
                    _collect_run_ui(run_ui)
                    if run_resp.get("deleted"):
                        approved_scripts.pop(target_rel, None)
                    db.add_message(
                        session.id,
                        role="tool_event",
                        text=json.dumps({
                            "tool_name": tool_name,
                            "call_id": f"bridge-{idx}",
                            "response": run_resp,
                        }),
                    )
                    auto_executed += 1
                except Exception as exc:
                    warnings.append(f"{tool_name}: {exc}")
                    error_count += 1
                    db.add_message(
                        session.id,
                        role="tool_event",
                        text=json.dumps({
                            "tool_name": tool_name,
                            "call_id": f"bridge-{idx}",
                            "response": {"error": str(exc), "target": target_rel},
                        }),
                    )
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
        bridge_reply_text = _maybe_append_scout_notice(
            db,
            db.get_session(session.id) or session,
            scout_enabled,
            bridge_svc.format_trailing_tool_events(messages),
        )
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
        "command_results": command_results,
        "command_result": command_results[-1] if command_results else None,
        "patch_record": last_patch_record,
        "generated_md_files": generated_md_files,
        "generated_md_path": generated_md_path,
        "generated_md_content": generated_md_content,
        "telemetry": local_scout.get_telemetry_snapshot(
            db, gemini, model=getattr(session, "model", None)
        ),
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
