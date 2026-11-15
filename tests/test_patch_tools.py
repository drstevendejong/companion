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


if __name__ == "__main__":
    unittest.main()
