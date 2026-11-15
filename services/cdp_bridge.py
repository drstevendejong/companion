"""
services/cdp_bridge.py - Chrome DevTools Protocol (port 9222) bridge for tab listing,
PNG viewport/full-page screenshots, and console/exception log capture.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Dict, List

import websockets

from services.token_utils import estimate_image_tokens, estimate_tokens


class CdpConnectionError(RuntimeError):
    """Raised when Chrome CDP on the configured port is unreachable or returns an error."""


def list_cdp_tabs(cdp_port: int = 9222) -> List[Dict[str, Any]]:
    """Queries Chrome's /json/list HTTP endpoint for inspectable page targets."""
    url = f"http://127.0.0.1:{cdp_port}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=2.5) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CdpConnectionError(
            f"Could not reach Chrome debug port {cdp_port} ({exc}). "
            f"Launch Chrome with --remote-debugging-port={cdp_port}."
        ) from exc

    targets = json.loads(raw)
    pages: List[Dict[str, Any]] = []
    for t in targets:
        if t.get("type") != "page":
            continue
        ws_url = t.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        pages.append({
            "id": t.get("id", ""),
            "title": t.get("title", "Untitled"),
            "url": t.get("url", ""),
            "ws_url": ws_url,
        })
    return pages


async def _cdp_call(ws: Any, req_id: int, method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = {"id": req_id, "method": method, "params": params or {}}
    await ws.send(json.dumps(payload))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=8.0)
        msg = json.loads(raw)
        if msg.get("id") == req_id:
            if "error" in msg:
                raise CdpConnectionError(f"CDP {method} failed: {msg['error']}")
            return msg.get("result", {})


async def capture_tab_screenshot(ws_url: str, full_page: bool = False) -> Dict[str, Any]:
    """Captures a PNG screenshot from the target Chrome tab via Page.captureScreenshot."""
    try:
        async with websockets.connect(ws_url, max_size=25 * 1024 * 1024) as ws:
            await _cdp_call(ws, 1, "Page.enable")
            params: Dict[str, Any] = {"format": "png"}
            if full_page:
                metrics = await _cdp_call(ws, 2, "Page.getLayoutMetrics")
                content_size = metrics.get("cssContentSize") or metrics.get("contentSize")
                if content_size:
                    params["clip"] = {
                        "x": 0,
                        "y": 0,
                        "width": content_size.get("width", 1280),
                        "height": content_size.get("height", 800),
                        "scale": 1,
                    }
            result = await _cdp_call(ws, 3, "Page.captureScreenshot", params)
    except Exception as exc:
        if isinstance(exc, CdpConnectionError):
            raise
        raise CdpConnectionError(f"CDP screenshot failed: {exc}") from exc

    b64_png = result.get("data", "")
    if not b64_png:
        raise CdpConnectionError("Chrome returned an empty screenshot.")
    return {
        "mime_type": "image/png",
        "base64_data": b64_png,
        "estimated_tokens": estimate_image_tokens(),
    }


def _format_remote_arg(arg: Dict[str, Any]) -> str:
    if "value" in arg:
        return repr(arg["value"]) if isinstance(arg["value"], str) else str(arg["value"])
    if "description" in arg:
        return str(arg["description"])
    return arg.get("type", "unknown")


async def fetch_tab_console(ws_url: str, drain_ms: int = 450) -> Dict[str, Any]:
    """
    Connects to the tab's Runtime + Log CDP domains and collects buffered console
    messages and uncaught exceptions.
    """
    entries: List[str] = []
    try:
        async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            await ws.send(json.dumps({"id": 2, "method": "Log.enable"}))

            deadline = asyncio.get_running_loop().time() + (drain_ms / 1000.0)
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break

                msg = json.loads(raw)
                method = msg.get("method")
                params = msg.get("params", {})

                if method == "Runtime.consoleAPICalled":
                    level = str(params.get("type", "log")).upper()
                    args = [_format_remote_arg(a) for a in params.get("args", [])]
                    entries.append(f"[{level}] {' '.join(args)}")
                elif method == "Runtime.exceptionThrown":
                    details = params.get("exceptionDetails", {})
                    text = details.get("text", "Uncaught exception")
                    exc_obj = details.get("exception", {})
                    desc = exc_obj.get("description", "")
                    line = details.get("lineNumber", "?")
                    url = details.get("url", "")
                    loc = f" ({url}:{line})" if url else ""
                    entries.append(f"[EXCEPTION]{loc} {text} {desc}".strip())
                elif method == "Log.entryAdded":
                    entry = params.get("entry", {})
                    level = str(entry.get("level", "info")).upper()
                    source = entry.get("source", "")
                    text = entry.get("text", "")
                    url = entry.get("url", "")
                    loc = f" ({url})" if url else ""
                    entries.append(f"[{level}:{source}]{loc} {text}".strip())
    except Exception as exc:
        raise CdpConnectionError(f"CDP console capture failed: {exc}") from exc

    formatted = "\n".join(entries) if entries else "(No console messages or exceptions in CDP buffer.)"
    return {
        "count": len(entries),
        "text": formatted,
        "estimated_tokens": estimate_tokens(formatted),
    }
