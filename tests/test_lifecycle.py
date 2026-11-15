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
        tracker = LifecycleTracker(grace_seconds=4.0, heartbeat_ttl_seconds=15.0)
        self.assertFalse(tracker.check_should_shutdown(now=100.0))
        self.assertFalse(tracker.check_should_shutdown(now=500.0))

    def test_refresh_cancels_bye_grace_window(self) -> None:
        tracker = LifecycleTracker(grace_seconds=4.0, heartbeat_ttl_seconds=15.0)
        tracker.heartbeat("tab-1", now=10.0)
        tracker.bye("tab-1", now=12.0)

        # Within grace period before reload connects -> not yet exited
        self.assertFalse(tracker.check_should_shutdown(now=13.0))

        # Browser reloads and registers a fresh tab ID -> cancels shutdown
        tracker.heartbeat("tab-2", now=13.2)
        self.assertFalse(tracker.check_should_shutdown(now=20.0))

    def test_multi_tab_and_final_tab_close_triggers_shutdown(self) -> None:
        tracker = LifecycleTracker(grace_seconds=4.0, heartbeat_ttl_seconds=15.0)
        tracker.heartbeat("tab-1", now=10.0)
        tracker.heartbeat("tab-2", now=10.5)

        # Closing one tab leaves the second tab alive
        tracker.bye("tab-1", now=11.0)
        self.assertFalse(tracker.check_should_shutdown(now=16.0))

        # Closing final tab triggers shutdown only after 4s grace
        tracker.bye("tab-2", now=16.5)
        self.assertFalse(tracker.check_should_shutdown(now=19.9))
        self.assertTrue(tracker.check_should_shutdown(now=20.6))

    def test_stale_heartbeat_ttl_triggers_shutdown(self) -> None:
        tracker = LifecycleTracker(grace_seconds=4.0, heartbeat_ttl_seconds=15.0)
        tracker.heartbeat("tab-1", now=0.0)
        self.assertFalse(tracker.check_should_shutdown(now=10.0))

        # Past 15s TTL: prunes stale client and starts 4s grace deadline
        self.assertFalse(tracker.check_should_shutdown(now=16.0))
        self.assertTrue(tracker.check_should_shutdown(now=20.1))


if __name__ == "__main__":
    unittest.main()
