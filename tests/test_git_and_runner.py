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


if __name__ == "__main__":
    unittest.main()
