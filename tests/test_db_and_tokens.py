"""Zero-token tests for token_utils, PackageManager, and SQLite CompanionDB."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))

from package_manager import PackageManager
from services.token_utils import estimate_tokens, truncate_head_tail

HAS_PYDANTIC = importlib.util.find_spec("pydantic") is not None
if HAS_PYDANTIC:
    from models import ProjectCreate, SessionCreate, TokenStats
    from storage.db import CompanionDB


class TestTokensAndPackageManager(unittest.TestCase):
    def test_token_estimation_and_truncation(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreater(estimate_tokens("def foo(): return 1"), 0)

        big = "A" * 2000 + "B" * 2000
        clipped, was_clipped = truncate_head_tail(big, max_chars=500)
        self.assertTrue(was_clipped)
        self.assertIn("omitted from middle", clipped)

    def test_package_manager_reads_pyproject(self) -> None:
        deps = PackageManager.get_core_dependencies()
        joined = " ".join(deps)
        self.assertIn("fastapi", joined)
        self.assertIn("google-genai", joined)


@unittest.skipUnless(HAS_PYDANTIC, "pydantic not installed yet (auto-installed at startup)")
class TestCompanionDB(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CompanionDB(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_project_session_message_and_token_accumulation(self) -> None:
        proj = self.db.create_project(
            ProjectCreate(name="Demo", root_path=self.tmp.name, backup_dir="backup")
        )
        sess = self.db.create_session(SessionCreate(project_id=proj.id, title="Initial"))
        self.db.add_message(sess.id, role="user", text="Hello")
        updated = self.db.add_session_tokens(
            sess.id, TokenStats(input_tokens=100, output_tokens=25, thinking_tokens=10, total_tokens=135)
        )
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.token_stats.total_tokens, 135)
        self.assertEqual(len(self.db.list_messages(sess.id)), 1)


if __name__ == "__main__":
    unittest.main()
