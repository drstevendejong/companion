"""
services/lifecycle.py - Browser tab heartbeat & auto-shutdown tracker.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional


class LifecycleTracker:
    """
    Tracks connected browser tabs so main.py exits cleanly when all tabs close,
    while allowing brief grace across F5 / Ctrl+Shift+R reloads.
    """

    def __init__(
        self,
        grace_seconds: float = 4.0,
        heartbeat_ttl_seconds: float = 15.0,
        enabled: bool = True,
    ) -> None:
        self.grace_seconds = grace_seconds
        self.heartbeat_ttl_seconds = heartbeat_ttl_seconds
        self.enabled = enabled
        self._lock = threading.Lock()
        self._clients: Dict[str, float] = {}
        self._seen_any: bool = False
        self._grace_deadline: Optional[float] = None

    def heartbeat(self, client_id: str, now: Optional[float] = None) -> int:
        if not client_id:
            return len(self._clients)
        ts = time.monotonic() if now is None else now
        with self._lock:
            self._clients[client_id] = ts
            self._seen_any = True
            self._grace_deadline = None
            return len(self._clients)

    def bye(self, client_id: str, now: Optional[float] = None) -> int:
        ts = time.monotonic() if now is None else now
        with self._lock:
            if client_id:
                self._clients.pop(client_id, None)
            if self._seen_any and not self._clients and self._grace_deadline is None:
                self._grace_deadline = ts + self.grace_seconds
            return len(self._clients)

    def check_should_shutdown(self, now: Optional[float] = None) -> bool:
        if not self.enabled:
            return False
        ts = time.monotonic() if now is None else now
        with self._lock:
            stale = [
                cid
                for cid, last_seen in self._clients.items()
                if (ts - last_seen) > self.heartbeat_ttl_seconds
            ]
            for cid in stale:
                self._clients.pop(cid, None)

            if not self._seen_any:
                return False

            if self._clients:
                self._grace_deadline = None
                return False

            if self._grace_deadline is None:
                self._grace_deadline = ts + self.grace_seconds
                return False

            return ts >= self._grace_deadline
