"""Zero-token unit tests for services/fs_tools.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))

from services import fs_tools


class TestFsTools(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("secret", encoding="utf-8")
        (self.root / "backup").mkdir()
        (self.root / "backup" / "old.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "src" / "alpha.py").write_text(
            "def greet(name: str) -> str:\n"
            "    return f'hello {name}'\n\n"
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sandbox_blocks_traversal(self) -> None:
        with self.assertRaises(fs_tools.PathSandboxError):
            fs_tools.resolve_safe_path(self.root, "../outside.txt")

    def test_list_tree_ignores_git_and_backup(self) -> None:
        res = fs_tools.list_tree(self.root, max_depth=3)
        paths = [e["path"] for e in res["entries"]]
        self.assertIn("src/alpha.py", paths)
        self.assertNotIn(".git", paths)
        self.assertNotIn("backup", paths)
        self.assertGreater(res["estimated_tokens"], 0)

        shallow = fs_tools.list_tree(self.root, max_depth=0)
        shallow_paths = [e["path"] for e in shallow["entries"]]
        self.assertEqual(shallow_paths, ["src"])

    def test_search_code_literal_and_regex(self) -> None:
        lit = fs_tools.search_code(self.root, "def greet", is_regex=False, context_lines=1)
        self.assertEqual(lit["total_hits"], 1)
        self.assertIn("src/alpha.py", lit["formatted"])

        reg = fs_tools.search_code(self.root, r"def \w+\(", is_regex=True, path_glob="*.py")
        self.assertEqual(reg["total_hits"], 2)

    def test_read_lines_and_full_file(self) -> None:
        slice_res = fs_tools.read_lines(self.root, "src/alpha.py", start_line=4, end_line=5)
        self.assertEqual(slice_res["start_line"], 4)
        self.assertEqual(slice_res["end_line"], 5)
        self.assertIn("4: def add", slice_res["content"])
        self.assertNotIn("def greet", slice_res["content"])

        full_res = fs_tools.read_file(self.root, "src/alpha.py")
        self.assertEqual(full_res["total_lines"], 5)
        self.assertTrue(full_res["content"].startswith("def greet"))
        self.assertNotIn("1: def greet", full_res["content"])

    def test_read_file_allows_large_files_without_truncation(self) -> None:
        big_text = ("# packed code line\n" * 10_000)  # ~190 KB
        (self.root / "packed_bundle.md").write_text(big_text, encoding="utf-8")
        res = fs_tools.read_file(self.root, "packed_bundle.md")
        self.assertFalse(res["truncated"])
        self.assertEqual(res["char_count"], len(big_text))
        self.assertEqual(res["content"], big_text)


if __name__ == "__main__":
    unittest.main()
