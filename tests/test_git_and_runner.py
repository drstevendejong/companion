"""Zero-token unit tests for services/git_tools.py and services/runner.py."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))

from services import git_tools, runner


class TestGitAndRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_runner_executes_python_script(self) -> None:
        script = self.root / "hello.py"
        script.write_text("print('TEST_OK_42')\n", encoding="utf-8")
        res = asyncio.run(runner.run_project_command(self.root, "python hello.py"))
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("TEST_OK_42", res["output"])
        self.assertGreater(res["estimated_tokens"], 0)

    def test_runner_streams_python_stdout_unbuffered(self) -> None:
        import time

        script = self.root / "stream_test.py"
        script.write_text(
            "import time\n"
            "print('FIRST_CHUNK_READY')\n"
            "time.sleep(0.35)\n"
            "print('SECOND_CHUNK_READY')\n",
            encoding="utf-8",
        )
        seen: list[tuple[float, str]] = []
        t0 = time.monotonic()

        async def _on_out(chunk: str) -> None:
            seen.append((time.monotonic() - t0, chunk))

        res = asyncio.run(
            runner.run_project_command(self.root, "python stream_test.py", on_output=_on_out)
        )
        self.assertEqual(res["exit_code"], 0)
        first_times = [dt for dt, txt in seen if "FIRST_CHUNK_READY" in txt]
        second_times = [dt for dt, txt in seen if "SECOND_CHUNK_READY" in txt]
        self.assertTrue(first_times, f"Missing first chunk in {seen!r}")
        self.assertTrue(second_times, f"Missing second chunk in {seen!r}")
        self.assertGreaterEqual(second_times[0] - first_times[0], 0.15)

    def test_git_status_and_commit_in_temp_repo(self) -> None:
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.root, check=True)

        f = self.root / "app.py"
        f.write_text("print('v1')\n", encoding="utf-8")

        status = git_tools.get_git_status(self.root)
        self.assertTrue(status["is_repo"])
        self.assertEqual(len(status["changed_files"]), 1)

        committed = git_tools.commit_changes(self.root, "feat: initial commit", push=False)
        self.assertTrue(committed["committed"])

        clean = git_tools.get_git_status(self.root)
        self.assertEqual(clean["changed_files"], [])

    def test_runner_captures_stderr_only_traceback(self) -> None:
        script = self.root / "stderr_boom.py"
        script.write_text(
            "import re\n"
            "re.compile(None)\n",
            encoding="utf-8",
        )
        chunks: list[str] = []

        async def _on_out(chunk: str) -> None:
            chunks.append(chunk)

        res = asyncio.run(
            runner.run_project_command(self.root, "python stderr_boom.py", on_output=_on_out)
        )
        self.assertNotEqual(res["exit_code"], 0)
        self.assertFalse(res["cancelled"])
        self.assertFalse(res["timed_out"])
        self.assertIn("TypeError", res["output"])
        self.assertIn("Traceback", "".join(chunks))

    def test_runner_does_not_hang_on_stdin_read(self) -> None:
        script = self.root / "reads_stdin.py"
        script.write_text(
            "import sys\n"
            "data = sys.stdin.read()\n"
            "print(f'STDIN_LEN={len(data)}')\n",
            encoding="utf-8",
        )
        res = asyncio.run(
            runner.run_project_command(self.root, "python reads_stdin.py", timeout_seconds=5)
        )
        self.assertEqual(res["exit_code"], 0)
        self.assertFalse(res["timed_out"])
        self.assertIn("STDIN_LEN=0", res["output"])

    def test_runner_cancel_active_runs_stops_sleeping_process(self) -> None:
        import time
        from types import SimpleNamespace

        script = self.root / "sleeper.py"
        script.write_text(
            "import time\n"
            "print('STARTED_SLEEP', flush=True)\n"
            "time.sleep(15)\n"
            "print('SHOULD_NOT_PRINT', flush=True)\n",
            encoding="utf-8",
        )
        ws_events: list[dict] = []

        class _WS:
            async def send_text(self, raw: str) -> None:
                import json

                ws_events.append(json.loads(raw))

        app = SimpleNamespace(state=SimpleNamespace(lifecycle_sockets={_WS()}))

        async def _run_and_cancel() -> dict:
            task = asyncio.create_task(
                runner.stream_project_command(
                    app,
                    self.root,
                    "python sleeper.py",
                    session_id="s-cancel",
                    gate_id="g-cancel",
                    source="gate",
                )
            )
            for _ in range(40):
                if runner.get_active_runs(session_id="s-cancel"):
                    break
                await asyncio.sleep(0.05)
            cancelled_count = runner.cancel_active_runs(session_id="s-cancel")
            self.assertGreaterEqual(cancelled_count, 1)
            return await task

        t0 = time.monotonic()
        res = asyncio.run(_run_and_cancel())
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 4.0)
        self.assertTrue(res["cancelled"])
        self.assertEqual(res["exit_code"], -1)
        self.assertNotIn("SHOULD_NOT_PRINT", res["output"])
        self.assertTrue(
            any(ev.get("type") == "exec_done" and ev.get("cancelled") is True for ev in ws_events)
        )

    def test_runner_dead_websocket_does_not_block(self) -> None:
        import json
        import time
        from types import SimpleNamespace

        script = self.root / "quick.py"
        script.write_text("print('QUICK_DONE')\n", encoding="utf-8")
        good_events: list[dict] = []

        class _HangingWS:
            async def send_text(self, _raw: str) -> None:
                await asyncio.sleep(15)

        class _BrokenWS:
            async def send_text(self, _raw: str) -> None:
                raise RuntimeError("socket closed")

        class _GoodWS:
            async def send_text(self, raw: str) -> None:
                good_events.append(json.loads(raw))

        hanging_ws = _HangingWS()
        broken_ws = _BrokenWS()
        good_ws = _GoodWS()
        sockets = {hanging_ws, broken_ws, good_ws}
        app = SimpleNamespace(state=SimpleNamespace(lifecycle_sockets=sockets))

        t0 = time.monotonic()
        res = asyncio.run(
            runner.stream_project_command(
                app,
                self.root,
                "python quick.py",
                session_id="s-ws",
                source="gate",
            )
        )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 4.0)
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("QUICK_DONE", res["output"])
        self.assertNotIn(hanging_ws, sockets)
        self.assertNotIn(broken_ws, sockets)
        self.assertIn(good_ws, sockets)
        self.assertTrue(any(ev.get("type") == "exec_done" for ev in good_events))


if __name__ == "__main__":
    unittest.main()
