"""
main.py - Startup entrypoint with upfront PackageManager check, Chrome auto-launch,
and clean auto-shutdown when the Companion browser tab is closed.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time

from package_manager import PackageManager


def _is_port_in_use(host: str, port: int) -> bool:
    check_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((check_host, port)) == 0


def _start_shutdown_watchdog(app: object, server: object) -> None:
    def _watch() -> None:
        prev_tick = time.monotonic()
        while not getattr(server, "should_exit", False):
            time.sleep(0.5)
            now = time.monotonic()
            dt = now - prev_tick
            prev_tick = now
            if dt > 5.0:
                app.state.lifecycle.on_system_wake(dt, now=now)
            if app.state.lifecycle.check_should_shutdown(now=now):
                logging.info("Companion browser tab closed — shutting down server cleanly.")
                server.should_exit = True
                return

    threading.Thread(target=_watch, daemon=True, name="lifecycle-watchdog").start()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)

    if not PackageManager.ensure_core_dependencies(auto_install=True):
        logging.error("Failed to verify or install required dependencies from pyproject.toml.")
        sys.exit(1)

    import uvicorn
    from app import create_app
    from config import DEFAULT_HOST, DEFAULT_PORT
    from services.browser_launcher import launch_chrome_when_ready

    if _is_port_in_use(DEFAULT_HOST, DEFAULT_PORT):
        logging.error(
            "Port %s on %s is already in use by an earlier Gemini Companion server. "
            "Stop the existing python process first (or set COMPANION_PORT to another port) and restart.",
            DEFAULT_PORT,
            DEFAULT_HOST,
        )
        sys.exit(1)

    no_browser = "--no-browser" in sys.argv or os.environ.get("COMPANION_NO_BROWSER") == "1"
    auto_shutdown = (
        "--no-auto-shutdown" not in sys.argv
        and os.environ.get("COMPANION_AUTO_SHUTDOWN", "1") != "0"
    )

    if not no_browser:
        launch_chrome_when_ready(DEFAULT_HOST, DEFAULT_PORT)

    app = create_app(auto_shutdown=auto_shutdown)
    config = uvicorn.Config(app, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    if auto_shutdown:
        _start_shutdown_watchdog(app, server)
    server.run()


if __name__ == "__main__":
    main()
