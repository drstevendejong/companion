"""Zero-token contract tests ensuring static/index.html defines every ID referenced by static/js/*.js."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

COMPANION_ROOT = Path(__file__).resolve().parent.parent


class TestUiDomContract(unittest.TestCase):
    def test_all_js_getelementbyid_exist_in_index_html(self) -> None:
        html = (COMPANION_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        html_ids = set(re.findall(r'id="([^"]+)"', html))

        referenced: dict[str, list[str]] = {}
        for js_path in sorted((COMPANION_ROOT / "static" / "js").glob("*.js")):
            text = js_path.read_text(encoding="utf-8")
            for match in re.finditer(r'getElementById\(["\']([^"\']+)["\']\)', text):
                referenced.setdefault(match.group(1), []).append(js_path.name)

        self.assertGreater(len(referenced), 50)
        missing = {k: v for k, v in sorted(referenced.items()) if k not in html_ids}
        self.assertEqual(missing, {}, f"JS files reference missing HTML IDs: {missing}")


if __name__ == "__main__":
    unittest.main()
