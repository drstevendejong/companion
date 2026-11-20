"""
services/lifecycle.py - Browser tab lifecycle tracker using persistent WebSockets
(immune to Chrome background-tab timer throttling) with HTTP heartbeat fallback.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Set


class LifecycleTracker:
    """
    Tracks open browser tabs so main.py exits cleanly when all tabs close,
    while staying immune to Chrome background-tab throttling, laptop sleep/wake
    gaps, and short F5 / Ctrl+Shift+R reloads.
    """

    def __init__(
        self,
        grace_seconds: float = 4.0,
        heartbeat_ttl_seconds: float = 300.0,
        sleep_ttl_seconds: float = 600.0,
        enabled: bool = True,
    ) -> None:
        self.grace_seconds = grace_seconds
        self.heartbeat_ttl_seconds = heartbeat_ttl_seconds
        self.sleep_ttl_seconds = sleep_ttl_seconds
        self.enabled = enabled
        self._lock = threading.Lock()
        self._ws_clients: Set[str] = set()
        self._http_clients: Dict[str, float] = {}
        self._sleeping_clients: Dict[str, float] = {}
        self._closed_clients: Dict[str, float] = {}
        self._seen_any: bool = False
        self._grace_deadline: Optional[float] = None

    def _active_total_locked(self) -> int:
        return len(self._ws_clients) + len(self._http_clients) + len(self._sleeping_clients)

    def ws_connect(self, client_id: str) -> int:
        if not client_id:
            return self._active_total_locked()
        with self._lock:
            self._ws_clients.add(client_id)
            self._http_clients.pop(client_id, None)
            self._sleeping_clients.pop(client_id, None)
            self._closed_clients.pop(client_id, None)
            self._seen_any = True
            self._grace_deadline = None
            return self._active_total_locked()

    def ws_disconnect(
        self,
        client_id: str,
        now: Optional[float] = None,
        explicit: bool = True,
    ) -> int:
        ts = time.monotonic() if now is None else now
        with self._lock:
            if client_id:
                self._ws_clients.discard(client_id)
                self._http_clients.pop(client_id, None)
                if explicit:
                    self._sleeping_clients.pop(client_id, None)
                    self._closed_clients[client_id] = ts
                elif client_id not in self._closed_clients:
                    # Passive transport drop (e.g., laptop sleep): hold awake grace instead of killing server
                    self._sleeping_clients[client_id] = ts

            total = self._active_total_locked()
            if self._seen_any and total == 0 and self._grace_deadline is None:
                self._grace_deadline = ts + self.grace_seconds
            return total

    def heartbeat(self, client_id: str, now: Optional[float] = None) -> int:
        if not client_id:
            return self._active_total_locked()
        ts = time.monotonic() if now is None else now
        with self._lock:
            self._closed_clients.pop(client_id, None)
            self._sleeping_clients.pop(client_id, None)
            if client_id not in self._ws_clients:
                self._http_clients[client_id] = ts
            self._seen_any = True
            self._grace_deadline = None
            return self._active_total_locked()

    def bye(self, client_id: str, now: Optional[float] = None) -> int:
        return self.ws_disconnect(client_id, now=now, explicit=True)

    def on_system_wake(self, sleep_gap: float, now: Optional[float] = None) -> None:
        """Shifts awake TTL timestamps forward after an OS sleep/suspend gap."""
        if sleep_gap <= 0:
            return
        ts = time.monotonic() if now is None else now
        with self._lock:
            for cid in list(self._http_clients.keys()):
                self._http_clients[cid] = min(ts, self._http_clients[cid] + sleep_gap)
            for cid in list(self._sleeping_clients.keys()):
                self._sleeping_clients[cid] = min(ts, self._sleeping_clients[cid] + sleep_gap)
            if self._grace_deadline is not None:
                self._grace_deadline = max(self._grace_deadline + sleep_gap, ts + 45.0)

    def check_should_shutdown(self, now: Optional[float] = None) -> bool:
        if not self.enabled:
            return False
        ts = time.monotonic() if now is None else now
        with self._lock:
            stale_http = [
                cid
                for cid, last_seen in self._http_clients.items()
                if (ts - last_seen) > self.heartbeat_ttl_seconds
            ]
            for cid in stale_http:
                self._http_clients.pop(cid, None)

            stale_sleeping = [
                cid
                for cid, last_seen in self._sleeping_clients.items()
                if (ts - last_seen) > self.sleep_ttl_seconds
            ]
            for cid in stale_sleeping:
                self._sleeping_clients.pop(cid, None)

            stale_closed = [
                cid for cid, closed_at in self._closed_clients.items() if (ts - closed_at) > 300.0
            ]
            for cid in stale_closed:
                self._closed_clients.pop(cid, None)

            if not self._seen_any:
                return False

            if self._ws_clients or self._http_clients or self._sleeping_clients:
                self._grace_deadline = None
                return False

            if self._grace_deadline is None:
                self._grace_deadline = ts + self.grace_seconds
                return False

            return ts >= self._grace_deadline
