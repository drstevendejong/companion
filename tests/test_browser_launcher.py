"""Zero-token unit tests for services/browser_launcher.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))

from services import browser_launcher


class TestBrowserLauncher(unittest.TestCase):
    def test_launches_chrome_when_executable_found_on_linux(self) -> None:
        with patch("services.browser_launcher.find_chrome_executable", return_value="/usr/bin/google-chrome"), \
             patch("services.browser_launcher.subprocess.Popen") as mock_popen:
            ok = browser_launcher.open_url_in_chrome("http://127.0.0.1:8765", platform_name="linux")
            self.assertTrue(ok)
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertEqual(args, ["/usr/bin/google-chrome", "http://127.0.0.1:8765"])

    def test_launches_chrome_via_open_on_macos(self) -> None:
        with patch(
            "services.browser_launcher.find_chrome_executable",
            return_value="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ), patch("services.browser_launcher.subprocess.Popen") as mock_popen:
            ok = browser_launcher.open_url_in_chrome("http://127.0.0.1:8765", platform_name="darwin")
            self.assertTrue(ok)
            args = mock_popen.call_args[0][0]
            self.assertEqual(args, ["open", "-a", "Google Chrome", "http://127.0.0.1:8765"])

    def test_falls_back_to_default_browser_when_chrome_missing(self) -> None:
        with patch("services.browser_launcher.find_chrome_executable", return_value=None), \
             patch("services.browser_launcher.webbrowser.open", return_value=True) as mock_web:
            ok = browser_launcher.open_url_in_chrome("http://127.0.0.1:8765", platform_name="linux")
            self.assertTrue(ok)
            mock_web.assert_called_once_with("http://127.0.0.1:8765")


if __name__ == "__main__":
    unittest.main()
