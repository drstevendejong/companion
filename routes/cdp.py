"""
routes/cdp.py - Chrome DevTools Protocol (port 9222) endpoints for tab listing,
screenshot capture, and console/exception log draining.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

from models import CdpTargetRequest
from services import cdp_bridge

router = APIRouter(prefix="/api/cdp", tags=["cdp"])


@router.get("/tabs")
def list_tabs(cdp_port: int = Query(default=9222, ge=1, le=65535)) -> Dict[str, Any]:
    try:
        tabs = cdp_bridge.list_cdp_tabs(cdp_port=cdp_port)
        return {"connected": True, "cdp_port": cdp_port, "tabs": tabs}
    except cdp_bridge.CdpConnectionError as exc:
        return {"connected": False, "cdp_port": cdp_port, "tabs": [], "error": str(exc)}


@router.post("/screenshot")
async def take_screenshot(payload: CdpTargetRequest) -> Dict[str, Any]:
    try:
        return await cdp_bridge.capture_tab_screenshot(
            payload.ws_url, full_page=payload.full_page
        )
    except cdp_bridge.CdpConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/console")
async def grab_console(payload: CdpTargetRequest) -> Dict[str, Any]:
    try:
        return await cdp_bridge.fetch_tab_console(payload.ws_url)
    except cdp_bridge.CdpConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
