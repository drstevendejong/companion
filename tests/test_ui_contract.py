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

    def test_collapsible_grid_and_pane_button_placement(self) -> None:
        html = (COMPANION_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        css = (COMPANION_ROOT / "static" / "css" / "layout.css").read_text(encoding="utf-8")

        for selector, col in (
            (".sidebar", "grid-column: 1"),
            ("#resizer-left", "grid-column: 2"),
            (".main-stage", "grid-column: 3"),
            ("#resizer-right", "grid-column: 4"),
            (".drawer", "grid-column: 5"),
        ):
            self.assertIn(selector, css)
            self.assertIn(col, css)

        self.assertIn(".app-shell:not(.sidebar-collapsed) #btn-expand-sidebar", css)
        self.assertIn(".app-shell:not(.drawer-collapsed) #btn-expand-drawer", css)

        sidebar_header = html.split('class="sidebar-header"', 1)[1].split("</aside>", 1)[0]
        topbar = html.split('class="topbar"', 1)[1].split("</header>", 1)[0]
        drawer_tabs = html.split('class="drawer-tabs"', 1)[1].split("</nav>", 1)[0]

        self.assertIn('id="btn-collapse-sidebar"', sidebar_header)
        self.assertIn('id="btn-collapse-drawer"', drawer_tabs)
        self.assertIn('id="btn-expand-sidebar"', topbar)
        self.assertIn('id="btn-expand-drawer"', topbar)

    def test_chat_stream_and_bridge_composer_contract(self) -> None:
        html = (COMPANION_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        css = (COMPANION_ROOT / "static" / "css" / "layout.css").read_text(encoding="utf-8")

        self.assertIn(".chat-stream > *", css)
        self.assertIn("flex-shrink: 0", css)
        for req_id in (
            "btn-bridge-directives-pill",
            "btn-bridge-manual-paste",
            "btn-bridge-close-paste",
            "btn-bridge-stage-clipboard",
            "btn-bridge-clipboard-parse",
            "bridge-prompt-slot",
            "bridge-paste-wrap",
            "bridge-paste-hint",
            "btn-bridge-parse-stage",
            "btn-bridge-parse",
            "btn-bridge-copy-bundle",
        ):
            self.assertIn(f'id="{req_id}"', html)
        for removed_id in ("chk-bridge-directives", "btn-bridge-tab-prompt", "btn-bridge-tab-paste"):
            self.assertNotIn(f'id="{removed_id}"', html)

        comp_css = (COMPANION_ROOT / "static" / "css" / "components.css").read_text(encoding="utf-8")
        self.assertIn(".gates-batch-bar", comp_css)
        self.assertIn("position: sticky", comp_css)
        self.assertIn("top: 0", comp_css)

        chat_js = (COMPANION_ROOT / "static" / "js" / "chat.js").read_text(encoding="utf-8")
        drawer_js = (COMPANION_ROOT / "static" / "js" / "drawer.js").read_text(encoding="utf-8")
        gates_js = (COMPANION_ROOT / "static" / "js" / "tool_gates.js").read_text(encoding="utf-8")
        bridge_js = (COMPANION_ROOT / "static" / "js" / "bridge.js").read_text(encoding="utf-8")
        self.assertIn("Copy Error", chat_js)
        self.assertIn("Copy Full Output", chat_js)
        self.assertIn("createGateVisibilityButton", chat_js)
        self.assertIn("buildCommandFailureReport", drawer_js)
        self.assertIn("extractFailureExcerpt", drawer_js)
        self.assertIn("gatesHiddenDueToError", gates_js)
        self.assertIn("buildErrorReasonForDeniedAction", bridge_js)

        api_js = (COMPANION_ROOT / "static" / "js" / "api.js").read_text(encoding="utf-8")
        self.assertIn(".msg-checkpoint", comp_css)
        self.assertIn("renderCheckpointCard", chat_js)
        self.assertIn("Commit Anyway", chat_js)
        self.assertIn("Restore", chat_js)
        self.assertIn("restoreCheckpoint", api_js)
        self.assertIn("commitCheckpoint", api_js)

    def test_theme_selector_and_css_tokens(self) -> None:
        html = (COMPANION_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        tokens_css = (COMPANION_ROOT / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
        theme_js = (COMPANION_ROOT / "static" / "js" / "theme.js").read_text(encoding="utf-8")

        self.assertIn('id="theme-select"', html)
        self.assertIn('class="sidebar-footer"', html)
        for theme_id in ("midnight", "darcula", "nordic-slate", "light", "warm-paper", "espresso", "forest-night"):
            self.assertIn(theme_id, theme_js)
            if theme_id != "midnight":
                self.assertIn(f'[data-theme="{theme_id}"]', tokens_css)


if __name__ == "__main__":
    unittest.main()
