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

    def test_search_code_generic_languages_and_brace_globs(self) -> None:
        (self.root / "app.js").write_text("function greet() { return 'hi'; }\n", encoding="utf-8")
        (self.root / "index.html").write_text("<script>greet();</script>\n", encoding="utf-8")
        (self.root / "lib.rs").write_text("fn greet() {}\n", encoding="utf-8")

        all_files = fs_tools.search_code(self.root, "greet", path_glob=None)
        self.assertEqual(all_files["total_hits"], 4)

        brace_glob = fs_tools.search_code(self.root, "greet", path_glob="*.{js,html}")
        matched_paths = {m["path"] for m in brace_glob["matches"]}
        self.assertEqual(matched_paths, {"app.js", "index.html"})

        comma_glob = fs_tools.search_code(self.root, "greet", path_glob="*.py, *.rs")
        matched_comma = {m["path"] for m in comma_glob["matches"]}
        self.assertEqual(matched_comma, {"src/alpha.py", "lib.rs"})

    def test_search_code_ignore_case_filenames_and_non_utf8(self) -> None:
        (self.root / "core").mkdir()
        (self.root / "core" / "surveillance_tab.py").write_bytes(
            b"# \xe9 Surveillance module\nSURVEILLANCE_KEY = 'ok'\n"
        )
        (self.root / "core" / "panel.html").write_text("<div id='Surveillance'></div>\n", encoding="utf-8")
        (self.root / "core" / "panel.css").write_text(".surveillance-box {}\n", encoding="utf-8")

        res_ic = fs_tools.search_code(
            self.root,
            "surveillance",
            path_glob="*.py,*.js,*.html,*.css,*.json",
            ignore_case=True,
        )
        # 1 filename + 2 content hits in surveillance_tab.py, 1 in panel.html, 1 in panel.css
        self.assertEqual(res_ic["total_hits"], 5)
        self.assertIn("[Filename match: core/surveillance_tab.py]", res_ic["formatted"])

        res_exact = fs_tools.search_code(
            self.root,
            "SURVEILLANCE",
            path_glob="*.py,*.js,*.html,*.css,*.json",
            ignore_case=False,
        )
        self.assertEqual(res_exact["total_hits"], 1)

    def test_list_tree_and_search_ignore_nested_dot_dirs_and_no_300_cap(self) -> None:
        (self.root / ".image_cache").mkdir()
        (self.root / ".image_cache" / "cached.png").write_bytes(b"png")
        (self.root / "data-moc" / ".image_cache").mkdir(parents=True)
        (self.root / "data-moc" / ".image_cache" / "surveillance.txt").write_text(
            "surveillance in cache\n", encoding="utf-8"
        )
        many_dir = self.root / "bulk"
        many_dir.mkdir()
        for i in range(340):
            (many_dir / f"f_{i:03d}.py").write_text("x = 1\n", encoding="utf-8")

        tree = fs_tools.list_tree(self.root, max_depth=4)
        self.assertFalse(tree["truncated"])
        self.assertNotIn("Tree truncated", tree["tree"])
        self.assertNotIn(".image_cache", tree["tree"])
        self.assertGreaterEqual(len(tree["entries"]), 342)

        hits = fs_tools.search_code(self.root, "surveillance")
        self.assertEqual(hits["total_hits"], 0)

    def test_search_code_returns_all_matches_without_40_cap(self) -> None:
        many_lines = "\n".join(f"item_{i} = 'surveillance_{i}'" for i in range(250)) + "\n"
        (self.root / "many.py").write_text(many_lines, encoding="utf-8")

        res = fs_tools.search_code(self.root, "surveillance", context_lines=0)
        self.assertEqual(res["total_hits"], 250)
        self.assertEqual(res["shown_hits"], 250)
        self.assertEqual(len(res["matches"]), 250)
        self.assertNotIn("more matches omitted", res["formatted"])
        self.assertIn("--- many.py:1-250 ---", res["formatted"])
        self.assertIn("item_249 = 'surveillance_249'", res["formatted"])

    def test_search_code_merges_overlapping_windows_skips_blank_lines_and_prefixes(self) -> None:
        (self.root / "merged.py").write_text(
            "def top():\n"
            "\n"
            "    alpha = 'surveillance_one'\n"
            "    beta = 'surveillance_two'\n"
            "\n"
            "    return beta\n",
            encoding="utf-8",
        )
        res = fs_tools.search_code(self.root, "surveillance", context_lines=2)
        self.assertEqual(res["total_hits"], 2)
        # Consecutive hits merge into a single hunk header with compact range
        self.assertEqual(res["formatted"].count("--- merged.py:"), 1)
        self.assertIn("--- merged.py:3-4 (L1-6) ---", res["formatted"])
        # No per-line '3: ' prefixes and no blank context lines
        self.assertNotIn("\n3: ", res["formatted"])
        self.assertEqual(
            res["formatted"].splitlines(),
            [
                "--- merged.py:3-4 (L1-6) ---",
                "def top():",
                "    alpha = 'surveillance_one'",
                "    beta = 'surveillance_two'",
                "    return beta",
            ],
        )

    def test_search_code_truncates_massive_single_line_around_match(self) -> None:
        prefix = '  <script id="diary-data" type="application/json">{"version":1,'
        filler_before = '"pad":"' + ("a" * 20_000) + '",'
        target = '"mode":"surveillance_tracker",'
        filler_after = '"tail":"' + ("b" * 65_000) + '"'
        postfix = ',"ok":true}</script>'
        massive_line = prefix + filler_before + target + filler_after + postfix
        (self.root / "dev_tool.html").write_text(f"<div>hi</div>\n{massive_line}\n", encoding="utf-8")

        res = fs_tools.search_code(self.root, "surveillance", context_lines=1)
        self.assertEqual(res["total_hits"], 1)
        formatted = res["formatted"]
        self.assertLess(len(formatted), 600)
        self.assertIn('<script id="diary-data"', formatted)
        self.assertIn('"mode":"surveillance_tracker"', formatted)
        self.assertIn(',"ok":true}</script>', formatted)
        self.assertIn("chars]...", formatted)

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

    def test_search_code_subpath_and_line_bounds(self) -> None:
        (self.root / "other").mkdir()
        (self.root / "other" / "beta.py").write_text(
            "def greet():\n    return 'other'\n", encoding="utf-8"
        )
        (self.root / "src" / "multi.py").write_text(
            "marker = 'top'\n"
            "x = 1\n"
            "y = 2\n"
            "marker = 'mid'\n"
            "z = 3\n"
            "marker = 'bot'\n",
            encoding="utf-8",
        )

        # Subpath directory scopes results only inside src/
        dir_hits = fs_tools.search_code(self.root, "greet", subpath="src")
        self.assertEqual({m["path"] for m in dir_hits["matches"]}, {"src/alpha.py"})

        # Subpath single file + line bounds
        bounded = fs_tools.search_code(
            self.root, "marker", subpath="src/multi.py", start_line=3, end_line=5, context_lines=0
        )
        self.assertEqual(bounded["total_hits"], 1)
        self.assertEqual(bounded["matches"][0]["line"], 4)

        with self.assertRaises(FileNotFoundError):
            fs_tools.search_code(self.root, "greet", subpath="nonexistent_dir")


if __name__ == "__main__":
    unittest.main()
