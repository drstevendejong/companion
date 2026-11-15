"""
services/browser_launcher.py - Cross-platform Chrome launcher once the local server is listening.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def find_chrome_executable(platform_name: Optional[str] = None) -> Optional[str]:
    """Locates Google Chrome / Chromium on Windows, macOS, or Linux."""
    plat = platform_name or sys.platform

    if plat.startswith("win"):
        which_chrome = shutil.which("chrome") or shutil.which("chrome.exe")
        if which_chrome:
            return which_chrome
        win_roots = [
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        for base in win_roots:
            if not base:
                continue
            cand = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if cand.is_file():
                return str(cand)
        return None

    if plat == "darwin":
        mac_candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
        ]
        for cand in mac_candidates:
            if cand.is_file():
                return str(cand)
        return None

    for cmd in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(cmd)
        if found:
            return found
    return None


def open_url_in_chrome(url: str, platform_name: Optional[str] = None) -> bool:
    """
    Opens url in Chrome (launching Chrome if not already running).
    Falls back to the system default browser if Chrome cannot be found.
    """
    plat = platform_name or sys.platform
    chrome_exe = find_chrome_executable(plat)

    if plat == "darwin" and chrome_exe:
        try:
            subprocess.Popen(
                ["open", "-a", "Google Chrome", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError as exc:
            logger.warning("[BrowserLauncher] macOS Chrome launch failed: %s", exc)

    if chrome_exe:
        try:
            popen_kwargs: dict = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if plat.startswith("win"):
                creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
                popen_kwargs["creationflags"] = creationflags
            else:
                popen_kwargs["start_new_session"] = True
            subprocess.Popen([chrome_exe, url], **popen_kwargs)
            return True
        except OSError as exc:
            logger.warning("[BrowserLauncher] Chrome launch failed (%s), falling back to default browser.", exc)

    logger.info("[BrowserLauncher] Chrome executable not found; opening default browser.")
    return webbrowser.open(url)


def launch_chrome_when_ready(host: str, port: int, timeout_seconds: float = 12.0) -> threading.Thread:
    """Polls until host:port accepts connections, then opens http://host:port in Chrome."""
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{connect_host}:{port}"

    def _wait_and_open() -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((connect_host, port), timeout=0.3):
                    open_url_in_chrome(url)
                    return
            except OSError:
                time.sleep(0.15)
        logger.warning("[BrowserLauncher] Server did not bind %s within %.1fs.", url, timeout_seconds)

    thread = threading.Thread(target=_wait_and_open, name="chrome-launcher", daemon=True)
    thread.start()
    return thread
