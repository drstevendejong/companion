"""Zero-token tests for browser-tied server auto-shutdown lifecycle."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))

from services.lifecycle import LifecycleTracker


class TestLifecycleTracker(unittest.TestCase):
    def test_waits_for_first_client_before_shutting_down(self) -> None:
        tracker = LifecycleTracker(grace_seconds=4.0, heartbeat_ttl_seconds=300.0)
        self.assertFalse(tracker.check_should_shutdown(now=100.0))
        self.assertFalse(tracker.check_should_shutdown(now=5000.0))

    def test_persistent_ws_survives_indefinitely_when_tab_in_background(self) -> None:
        tracker = LifecycleTracker(grace_seconds=4.0, heartbeat_ttl_seconds=300.0)
        tracker.ws_connect("tab-1")
        # Simulate user coding in PyCharm for hours with Chrome background-throttled
        self.assertFalse(tracker.check_should_shutdown(now=100.0))
        self.assertFalse(tracker.check_should_shutdown(now=10_000.0))

    def test_refresh_cancels_ws_disconnect_grace_window(self) -> None:
        tracker = LifecycleTracker(grace_seconds=4.0, heartbeat_ttl_seconds=300.0)
        tracker.ws_connect("tab-1")
        tracker.ws_disconnect("tab-1", now=12.0)

        # Within grace period before reload connects -> not yet exited
        self.assertFalse(tracker.check_should_shutdown(now=13.0))

        # Browser reloads and connects a fresh tab ID -> cancels shutdown
        tracker.ws_connect("tab-2")
        self.assertFalse(tracker.check_should_shutdown(now=200.0))

    def test_multi_tab_and_final_tab_close_triggers_shutdown(self) -> None:
        tracker = LifecycleTracker(grace_seconds=4.0, heartbeat_ttl_seconds=300.0)
        tracker.ws_connect("tab-1")
        tracker.ws_connect("tab-2")

        # Closing one tab leaves the second tab alive
        tracker.ws_disconnect("tab-1", now=11.0)
        self.assertFalse(tracker.check_should_shutdown(now=50.0))

        # Closing final tab triggers shutdown only after 4s grace
        tracker.ws_disconnect("tab-2", now=50.5)
        self.assertFalse(tracker.check_should_shutdown(now=54.0))
        self.assertTrue(tracker.check_should_shutdown(now=54.6))

    def test_http_fallback_stale_ttl_triggers_shutdown(self) -> None:
        tracker = LifecycleTracker(grace_seconds=4.0, heartbeat_ttl_seconds=300.0)
        tracker.heartbeat("tab-1", now=0.0)
        self.assertFalse(tracker.check_should_shutdown(now=250.0))

        # Past 300s fallback TTL: prunes stale client and starts 4s grace deadline
        self.assertFalse(tracker.check_should_shutdown(now=301.0))
        self.assertTrue(tracker.check_should_shutdown(now=305.1))

    def test_passive_ws_disconnect_and_laptop_sleep_keep_server_alive(self) -> None:
        tracker = LifecycleTracker(
            grace_seconds=4.0, heartbeat_ttl_seconds=300.0, sleep_ttl_seconds=600.0
        )
        tracker.ws_connect("tab-1")
        # Laptop sleeps -> TCP/WS drops passively (explicit=False), not tab-closed bye
        tracker.ws_disconnect("tab-1", now=100.0, explicit=False)
        self.assertFalse(tracker.check_should_shutdown(now=120.0))

        # Laptop sleeps for 2 hours (7200s) and wakes -> watchdog calls on_system_wake
        wake_now = 7320.0
        tracker.on_system_wake(sleep_gap=7200.0, now=wake_now)
        self.assertFalse(tracker.check_should_shutdown(now=wake_now))
        self.assertFalse(tracker.check_should_shutdown(now=wake_now + 30.0))

        # Tab wakes up and reconnects WebSocket
        tracker.ws_connect("tab-1")
        self.assertFalse(tracker.check_should_shutdown(now=wake_now + 500.0))

        # Explicit tab close afterwards still shuts down cleanly after grace window
        tracker.bye("tab-1", now=wake_now + 510.0)
        self.assertFalse(tracker.check_should_shutdown(now=wake_now + 512.0))
        self.assertTrue(tracker.check_should_shutdown(now=wake_now + 514.5))


if __name__ == "__main__":
    unittest.main()
