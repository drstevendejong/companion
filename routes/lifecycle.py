"""
routes/lifecycle.py - Browser tab heartbeat & unload beacon endpoints.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, Request

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
