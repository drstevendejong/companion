"""
routes/lifecycle.py - Browser tab WebSocket & fallback beacon lifecycle endpoints.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect

router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"])


async def _extract_client_id(request: Request) -> str:
    try:
        raw = await request.body()
        if not raw:
            return ""
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return str(data.get("client_id", "")).strip()
    except Exception:
        return ""


@router.websocket("/ws")
async def lifecycle_ws(
    websocket: WebSocket,
    client_id: str = Query(default=""),
) -> None:
    await websocket.accept()
    cid = client_id.strip() or f"ws-{id(websocket)}"
    tracker = websocket.app.state.lifecycle
    sockets = getattr(websocket.app.state, "lifecycle_sockets", None)
    if sockets is not None:
        sockets.add(websocket)
    tracker.ws_connect(cid)
    explicit_close = False
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=20.0)
            except asyncio.TimeoutError:
                await websocket.send_text('{"type":"ping"}')
    except WebSocketDisconnect as exc:
        explicit_close = getattr(exc, "code", None) == 1000
    except Exception:
        explicit_close = False
    finally:
        if sockets is not None:
            sockets.discard(websocket)
        tracker.ws_disconnect(cid, explicit=explicit_close)


@router.get("/ping")
def lifecycle_ping() -> Dict[str, bool]:
    return {"ok": True}


@router.post("/heartbeat")
async def lifecycle_heartbeat(request: Request) -> Dict[str, Any]:
    client_id = await _extract_client_id(request)
    count = request.app.state.lifecycle.heartbeat(client_id)
    return {"ok": True, "active_clients": count}


@router.post("/bye")
async def lifecycle_bye(request: Request) -> Dict[str, Any]:
    client_id = await _extract_client_id(request)
    count = request.app.state.lifecycle.bye(client_id)
    return {"ok": True, "active_clients": count}
