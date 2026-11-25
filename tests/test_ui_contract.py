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
            "bridge-copy-split",
            "btn-bridge-copy-menu-toggle",
            "bridge-copy-menu",
            "btn-bridge-copy-with-directives",
            "btn-bridge-skip-directives",
            "btn-bridge-close-paste",
            "btn-bridge-clipboard-parse",
            "bridge-prompt-slot",
            "bridge-prompt-detect-bar",
            "btn-prompt-run-detected",
            "btn-prompt-keep-text",
            "bridge-paste-wrap",
            "bridge-paste-hint",
            "btn-bridge-parse",
            "btn-bridge-copy-bundle",
            "terminal-savepoint-bar",
            "btn-terminal-commit-checkpoint",
            "btn-terminal-restore-checkpoint",
        ):
            self.assertIn(f'id="{req_id}"', html)
        for removed_id in (
            "chk-bridge-directives",
            "btn-bridge-tab-prompt",
            "btn-bridge-tab-paste",
            "btn-bridge-manual-paste",
            "btn-bridge-stage-clipboard",
            "btn-bridge-parse-stage",
        ):
            self.assertNotIn(f'id="{removed_id}"', html)
        self.assertIn('>Console</button>', html)

        comp_css = (COMPANION_ROOT / "static" / "css" / "components.css").read_text(encoding="utf-8")
        self.assertIn(".gates-batch-bar", comp_css)
        self.assertIn("position: sticky", comp_css)
        self.assertIn("top: 0", comp_css)
        self.assertIn(".bridge-detect-bar", comp_css)
        self.assertIn(".terminal-savepoint-bar", comp_css)

        chat_js = (COMPANION_ROOT / "static" / "js" / "chat.js").read_text(encoding="utf-8")
        drawer_js = (COMPANION_ROOT / "static" / "js" / "drawer.js").read_text(encoding="utf-8")
        gates_js = (COMPANION_ROOT / "static" / "js" / "tool_gates.js").read_text(encoding="utf-8")
        bridge_js = (COMPANION_ROOT / "static" / "js" / "bridge.js").read_text(encoding="utf-8")
        lifecycle_js = (COMPANION_ROOT / "static" / "js" / "lifecycle.js").read_text(encoding="utf-8")
        composer_js = (COMPANION_ROOT / "static" / "js" / "composer.js").read_text(encoding="utf-8")
        self.assertIn("Copy Error", chat_js)
        self.assertIn("Copy Full Output", chat_js)
        self.assertIn("Retry Script", chat_js)
        self.assertIn("createGateVisibilityButton", chat_js)
        self.assertIn("resolveActivityAfterCount", chat_js)
        self.assertIn("scrollChatToBottom", chat_js)
        self.assertIn("applyArrivalFlash", chat_js)
        self.assertIn("gateTrayShowingStatus", chat_js)
        self.assertIn(".msg-card.is-new-arrival", comp_css)
        self.assertIn("chat:live-exec", chat_js)
        self.assertIn("scout_phase", chat_js)
        self.assertIn("data-scout-elapsed", chat_js)
        self.assertIn("propose_inspection_script", chat_js)
        self.assertIn("buildCommandFailureReport", drawer_js)
        self.assertIn("extractFailureExcerpt", drawer_js)
        self.assertIn("afterMsgId", drawer_js)
        self.assertIn("terminal:stream-event", drawer_js)
        self.assertIn("terminal:stream-event", lifecycle_js)
        self.assertIn("gatesHiddenDueToError", gates_js)
        self.assertIn("gate-card-", gates_js)
        self.assertIn("data-live-status", gates_js)
        self.assertIn("propose_inspection_script", gates_js)
        self.assertNotIn("Approve & Auto-Approve", gates_js)
        self.assertIn("Run Script in Venv", gates_js)
        self.assertIn("Run & Delete on Pass", gates_js)
        self.assertIn("Write & Run (Keep on Disk)", gates_js)
        self.assertIn("Write to Disk Only", gates_js)
        runner_py = (COMPANION_ROOT / "services" / "runner.py").read_text(encoding="utf-8")
        self.assertIn("PYTHONUNBUFFERED", runner_py)
        self.assertIn("buildErrorReasonForDeniedAction", bridge_js)
        self.assertIn("looksLikeBridgeAiOutput", bridge_js)
        self.assertIn("isDetectedPromptAiPending", composer_js)

        api_js = (COMPANION_ROOT / "static" / "js" / "api.js").read_text(encoding="utf-8")
        settings_js = (COMPANION_ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
        git_js = (COMPANION_ROOT / "static" / "js" / "git.js").read_text(encoding="utf-8")
        self.assertIn(".msg-checkpoint", comp_css)
        self.assertIn(".scout-live-card", comp_css)
        self.assertIn(".scout-summary-banner", comp_css)
        self.assertIn("renderCheckpointCard", chat_js)
        self.assertIn("renderLiveScoutCard", chat_js)
        self.assertIn("scout:stream-event", chat_js)
        self.assertIn("scout:stream-event", lifecycle_js)
        self.assertIn("Commit Anyway", chat_js)
        self.assertIn("Restore", chat_js)
        self.assertIn("restoreCheckpoint", api_js)
        self.assertIn("commitCheckpoint", api_js)
        self.assertIn("getScoutSettings", api_js)
        self.assertIn("gitUpdateHeaders", api_js)
        self.assertIn('id="btn-scout-toggle"', html)
        self.assertIn('id="btn-scout-auto"', html)
        self.assertIn('id="btn-git-update-headers"', html)
        self.assertIn("btn-scout-toggle", settings_js)
        self.assertIn("btn-git-update-headers", git_js)
        self.assertIn("btn-companion-settings", settings_js)
        self.assertIn("save_as_file", gates_js)
        self.assertIn("intent_ambiguous", gates_js)
        self.assertIn("Run Temp Inspection (Delete on Pass)", gates_js)
        self.assertIn("Save as Code File", gates_js)
        self.assertIn("<<<<<<<", bridge_js)

        sessions_js = (COMPANION_ROOT / "static" / "js" / "sessions.js").read_text(encoding="utf-8")
        for panel_id in (
            "main-ai-block",
            "btn-main-ai-toggle",
            "main-stats-line",
            "btn-main-details",
            "scout-telemetry-block",
            "scout-panel-body",
            "scout-telemetry-model",
            "scout-stats-line",
            "btn-scout-details",
            "ai-details-popover",
            "cdp-controls-group",
        ):
            self.assertIn(f'id="{panel_id}"', html)
        api_block = html.split('id="api-sidebar-block"', 1)[1].split('id="bridge-sidebar-block"', 1)[0]
        self.assertNotIn('id="scout-telemetry-block"', api_block)
        # Ensure Run Tests moved into Console tab and Directives + CDP controls moved out of topbar
        topbar = html.split('class="topbar"', 1)[1].split("</header>", 1)[0]
        self.assertNotIn('id="btn-bridge-directives-pill"', topbar)
        self.assertNotIn('id="bridge-copy-split"', topbar)
        self.assertNotIn('id="cdp-tab-select"', topbar)
        self.assertNotIn('id="btn-scout-toggle"', topbar)
        tab_terminal = html.split('id="tab-terminal"', 1)[1].split('id="tab-git"', 1)[0]
        self.assertIn('id="btn-run-post-tests"', tab_terminal)
        self.assertIn(".ai-details-popover", comp_css)
        self.assertIn(".scout-model-list", comp_css)
        self.assertIn(".modal-wide", comp_css)
        self.assertIn(".ambiguous-path-row", comp_css)
        self.assertIn("renderScoutTelemetry", sessions_js)
        self.assertIn("toggleDetailsPopover", sessions_js)

    def test_theme_selector_and_css_tokens(self) -> None:
        html = (COMPANION_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        tokens_css = (COMPANION_ROOT / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
        theme_js = (COMPANION_ROOT / "static" / "js" / "theme.js").read_text(encoding="utf-8")

        self.assertIn('id="theme-select"', html)
        self.assertIn('class="sidebar-footer"', html)
        for req_settings_id in (
            "btn-companion-settings",
            "sidebar-theme-label",
            "companion-settings-modal",
            "companion-settings-form",
            "setting-bridge-guardrail",
            "setting-system-prompt",
            "setting-bridge-contract",
            "btn-reset-guardrail",
            "btn-reset-system-prompt",
            "btn-reset-bridge-contract",
            "btn-reset-all-settings",
            "btn-cancel-companion-settings",
            "btn-save-companion-settings",
        ):
            self.assertIn(f'id="{req_settings_id}"', html)
        self.assertIn("--flash-accent-ring", tokens_css)
        for theme_id in ("midnight", "darcula", "nordic-slate", "light", "warm-paper", "espresso", "forest-night"):
            self.assertIn(theme_id, theme_js)
            if theme_id != "midnight":
                self.assertIn(f'[data-theme="{theme_id}"]', tokens_css)


if __name__ == "__main__":
    unittest.main()
