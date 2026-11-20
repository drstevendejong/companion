"""Zero-token unit tests for services/patch_tools.py (3-tier patching, AST guard, project backup/undo)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))

from services import patch_tools


class TestPatchTools(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "module.py"
        self.target.write_text(
            "def calc(x: int) -> int:\n"
            "    return x + 1\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_literal_patch_creates_project_backup_and_can_undo(self) -> None:
        preview = patch_tools.preview_literal_patch(
            self.root, "module.py", "return x + 1", "return x + 42"
        )
        self.assertTrue(preview["ast_valid"])
        self.assertIn("+    return x + 42", preview["diff"])

        applied = patch_tools.apply_prepared_patch(self.root, "backup", preview)
        self.assertIn("return x + 42", self.target.read_text(encoding="utf-8"))
        self.assertIsNotNone(applied["backup_path"])
        backup_path = Path(applied["backup_path"])
        self.assertTrue(backup_path.is_file())
        self.assertTrue(str(backup_path).startswith(str((self.root / "backup").resolve())))

        undo_res = patch_tools.undo_patch_on_disk(
            self.root, "module.py", applied["backup_path"], existed_before=True
        )
        self.assertEqual(undo_res["action"], "restored_from_backup")
        self.assertIn("return x + 1", self.target.read_text(encoding="utf-8"))

    def test_ast_validation_rejects_broken_python(self) -> None:
        with self.assertRaises(patch_tools.PatchValidationError):
            patch_tools.preview_literal_patch(
                self.root, "module.py", "return x + 1", "return (x +"
            )

    def test_line_patch_and_new_file_undo(self) -> None:
        line_preview = patch_tools.preview_line_patch(
            self.root, "module.py", 2, 2, "    return x * 10"
        )
        patch_tools.apply_prepared_patch(self.root, "backup", line_preview)
        self.assertIn("return x * 10", self.target.read_text(encoding="utf-8"))

        new_preview = patch_tools.preview_full_write(
            self.root, "new_helper.py", "VALUE = 7\n"
        )
        applied_new = patch_tools.apply_prepared_patch(self.root, "backup", new_preview)
        new_file = self.root / "new_helper.py"
        self.assertTrue(new_file.is_file())
        self.assertIsNone(applied_new["backup_path"])

        undo_new = patch_tools.undo_patch_on_disk(
            self.root, "new_helper.py", None, existed_before=False
        )
        self.assertEqual(undo_new["action"], "removed_new_file")
        self.assertFalse(new_file.exists())

    def test_backup_dir_self_cleans_by_count_age_and_size(self) -> None:
        import os
        import time

        backup_root = self.root / "backup"
        # Create 6 timestamped snapshots
        for idx in range(6):
            snap = backup_root / f"20261117_00000{idx}_000001"
            snap.mkdir(parents=True, exist_ok=True)
            f = snap / "module.py"
            f.write_text(f"v={idx}\n" + ("x" * 200), encoding="utf-8")

        # Prune down to max_snapshots=3 -> keeps 3 newest (000005, 000004, 000003)
        stats = patch_tools.prune_backup_directory(
            self.root, "backup", max_snapshots=3, max_age_days=30, max_bytes=10_000_000, min_keep=1
        )
        self.assertEqual(stats["kept_count"], 3)
        self.assertEqual(stats["removed_count"], 3)
        remaining = sorted(p.name for p in backup_root.iterdir() if p.is_dir())
        self.assertEqual(
            remaining,
            ["20261117_000003_000001", "20261117_000004_000001", "20261117_000005_000001"],
        )

        # Age test: mark oldest remaining snapshot as 15 days old (beyond min_keep=1 and max_age_days=7)
        now = time.time()
        old_snap = backup_root / "20261117_000003_000001"
        os.utime(old_snap, (now - 15 * 86400, now - 15 * 86400))
        stats_age = patch_tools.prune_backup_directory(
            self.root, "backup", max_snapshots=10, max_age_days=7, max_bytes=10_000_000, min_keep=1, now_ts=now
        )
        self.assertFalse(old_snap.exists())
        self.assertEqual(stats_age["kept_count"], 2)

        # Size test: cap total bytes so only newest 1 snapshot fits
        stats_size = patch_tools.prune_backup_directory(
            self.root, "backup", max_snapshots=10, max_age_days=30, max_bytes=250, min_keep=1, now_ts=now
        )
        self.assertEqual(stats_size["kept_count"], 1)
        self.assertTrue((backup_root / "20261117_000005_000001").is_dir())

    def test_literal_not_found_diagnostics_and_multi_match_lines(self) -> None:
        (self.root / "sample.py").write_text(
            "def first():\n"
            "    value = 10\n"
            "    return value + 1\n\n"
            "def second():\n"
            "    value = 10\n"
            "    return value + 2\n",
            encoding="utf-8",
        )

        # 1. Indentation/whitespace mismatch triggers line-range + actual-lines hint
        with self.assertRaises(patch_tools.PatchValidationError) as ctx_ws:
            patch_tools.preview_literal_patch(
                self.root,
                "sample.py",
                "  value = 10\n  return value + 1",
                "    value = 99\n    return value + 1",
            )
        msg_ws = str(ctx_ws.exception)
        self.assertIn("Found matching lines at 2–3", msg_ws)
        self.assertIn("Actual lines 2–3:", msg_ws)
        self.assertIn("2:     value = 10", msg_ws)

        # 2. Near match triggers difflib nearest-match range + diff + actual lines
        with self.assertRaises(patch_tools.PatchValidationError) as ctx_near:
            patch_tools.preview_literal_patch(
                self.root,
                "sample.py",
                "def second():\n    value = 999\n    return value + 2",
                "def second():\n    return 0",
            )
        msg_near = str(ctx_near.exception)
        self.assertIn("Nearest match around lines 5–7", msg_near)
        self.assertIn("expected (old_text)", msg_near)
        self.assertIn("-    value = 999", msg_near)
        self.assertIn("+    value = 10", msg_near)

        # 3. Duplicate occurrence reports starting line numbers
        with self.assertRaises(patch_tools.PatchValidationError) as ctx_dup:
            patch_tools.preview_literal_patch(
                self.root,
                "sample.py",
                "    value = 10",
                "    value = 20",
            )
        msg_dup = str(ctx_dup.exception)
        self.assertIn("matched 2 times", msg_dup)
        self.assertIn("lines 2, 6", msg_dup)

    def test_delete_and_rename_file_backup_and_validation(self) -> None:
        (self.root / "obsolete.py").write_text("OLD_FLAG = True\n", encoding="utf-8")
        del_prev = patch_tools.preview_delete_file(self.root, "obsolete.py")
        self.assertEqual(del_prev["action"], "delete")
        self.assertIn("-OLD_FLAG = True", del_prev["diff"])

        del_res = patch_tools.apply_delete_file(self.root, "backup", del_prev)
        self.assertTrue(del_res["deleted"])
        self.assertFalse((self.root / "obsolete.py").exists())
        self.assertIsNotNone(del_res["backup_path"])

        # Undo restores the deleted file from backup/
        undo_del = patch_tools.undo_patch_on_disk(
            self.root, "obsolete.py", del_res["backup_path"], existed_before=True
        )
        self.assertEqual(undo_del["action"], "restored_from_backup")
        self.assertEqual((self.root / "obsolete.py").read_text(encoding="utf-8"), "OLD_FLAG = True\n")

        # Rename / move with parent dir creation and overwrite protection
        ren_prev = patch_tools.preview_rename_file(self.root, "obsolete.py", "archive/legacy.py")
        self.assertEqual(ren_prev["action"], "rename")
        self.assertIn("rename obsolete.py -> archive/legacy.py", ren_prev["diff"])
        ren_res = patch_tools.apply_rename_file(self.root, "backup", ren_prev)
        self.assertTrue(ren_res["renamed"])
        self.assertFalse((self.root / "obsolete.py").exists())
        self.assertEqual((self.root / "archive" / "legacy.py").read_text(encoding="utf-8"), "OLD_FLAG = True\n")

        # Overwrite blocked by default when target already exists
        (self.root / "collision.py").write_text("X = 1\n", encoding="utf-8")
        with self.assertRaises(patch_tools.PatchValidationError):
            patch_tools.preview_rename_file(self.root, "archive/legacy.py", "collision.py", overwrite=False)


if __name__ == "__main__":
    unittest.main()
