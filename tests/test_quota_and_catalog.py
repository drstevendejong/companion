"""Zero-token unit tests for Gemini quota/429 parser, live model catalog & free/paid probing,
SQLite last_context_tokens migration, and search_code gate preview scope labeling."""

from __future__ import annotations

import importlib
import sqlite3
import sys
import tempfile
import types as pytypes
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))

from services.model_catalog import (
    FREE_KEY_NOTICE,
    fetch_dynamic_model_catalog,
    record_model_runtime_error,
)
from services.quota_parser import analyze_gemini_error, format_gemini_error


def _make_stub_google_genai():
    class _GenerateContentConfig:
        def __init__(self, **_kwargs):
            pass

    genai_types = pytypes.ModuleType("google.genai.types")
    genai_types.GenerateContentConfig = _GenerateContentConfig
    genai_types.Schema = lambda **_kw: None
    genai_types.Tool = lambda **_kw: None
    genai_types.Type = SimpleNamespace(OBJECT="OBJECT", STRING="STRING", INTEGER="INTEGER", BOOLEAN="BOOLEAN")
    genai_types.ThinkingLevel = SimpleNamespace(LOW="LOW", MEDIUM="MEDIUM", HIGH="HIGH", XHIGH="XHIGH")
    genai_types.ThinkingConfig = lambda **_kw: None
    genai_types.FunctionDeclaration = lambda **_kw: None

    genai_mod = pytypes.ModuleType("google.genai")
    genai_mod.types = genai_types
    genai_mod.Client = object

    google_mod = pytypes.ModuleType("google")
    google_mod.genai = genai_mod
    return {
        "google": google_mod,
        "google.genai": genai_mod,
        "google.genai.types": genai_types,
    }


class TestQuotaParser(unittest.TestCase):
    def test_parses_per_minute_free_tier_429(self) -> None:
        raw = (
            "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Quota exceeded for metric: "
            "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count, limit: 250000, "
            "model: gemini-4-hydrogen. Please retry in 48.445107879s.', 'status': 'RESOURCE_EXHAUSTED', "
            "'details': [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{"
            "'quotaId': 'GenerateContentInputTokensPerModelPerMinute-FreeTier', "
            "'quotaDimensions': {'model': 'gemini-4-hydrogen'}, 'quotaValue': '250000'}]}, "
            "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '48s'}]}}"
        )
        info = analyze_gemini_error(raw)
        self.assertTrue(info["is_429"])
        self.assertTrue(info["is_free_tier"])
        self.assertFalse(info["is_paid_required"])
        self.assertEqual(info["window"], "minute")
        self.assertEqual(info["retry_seconds"], 49)
        self.assertEqual(info["quota_value"], 250000)

        msg = format_gemini_error(raw)
        self.assertIn("WAIT A MINUTE (~49s)", msg)
        self.assertIn("250,000", msg)
        self.assertIn("gemini-4-hydrogen", msg)

    def test_parses_per_day_429(self) -> None:
        raw = (
            "429 RESOURCE_EXHAUSTED: quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier, "
            "quotaValue: 1500, model: gemini-3.8-flash"
        )
        msg = format_gemini_error(raw)
        self.assertIn("WAIT A DAY", msg)

    def test_parses_paid_required_limit_zero(self) -> None:
        raw = (
            "429 RESOURCE_EXHAUSTED: Quota exceeded for metric: "
            "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 0, "
            "model: gemini-4-carbon, quotaValue: '0'"
        )
        info = analyze_gemini_error(raw)
        self.assertTrue(info["is_paid_required"])
        self.assertIn("PAID API KEY REQUIRED", format_gemini_error(raw))


class TestDynamicModelCatalog(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "model_catalog_cache.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _sample_live_models(self):
        return [
            SimpleNamespace(name="models/gemini-4-hydrogen", display_name="Gemini 4 Hydrogen", supported_actions=["generateContent"], input_token_limit=1048576),
            SimpleNamespace(name="models/gemini-4-argon", display_name="Gemini 4 Argon", supported_actions=["generateContent"], input_token_limit=1048576),
            SimpleNamespace(name="models/gemini-4-carbon", display_name="Gemini 4 Carbon", supported_actions=["generateContent"], input_token_limit=1048576),
            SimpleNamespace(name="models/gemini-3.8-flash", display_name="Gemini 3.8 Flash", supported_actions=["generateContent"], input_token_limit=1048576),
            SimpleNamespace(name="models/gemini-3.1-pro", display_name="Gemini 3.1 Pro", supported_actions=["generateContent"], input_token_limit=2097152),
            SimpleNamespace(name="models/gemini-5-nova", display_name="Gemini 5 Nova", supported_actions=["generateContent"], input_token_limit=1048576),
            SimpleNamespace(name="models/gemini-2.5-flash", display_name="Gemini 2.5 Flash", supported_actions=["generateContent"], input_token_limit=1048576),
            SimpleNamespace(name="models/gemini-2.5-flash-001", display_name="Gemini 2.5 Flash 001", supported_actions=["generateContent"], input_token_limit=1048576),
            SimpleNamespace(name="models/gemini-3.8-flash-tts", display_name="Gemini 3.8 Flash TTS", supported_actions=["generateContent"], input_token_limit=32000),
            SimpleNamespace(name="models/gemini-omni-1.2-flash", display_name="Gemini Omni 1.2", supported_actions=["generateContent"], input_token_limit=128000),
            SimpleNamespace(name="models/gemini-embedding-2", display_name="Embedding", supported_actions=["embedContent"], input_token_limit=8192),
        ]

    def test_free_tier_key_filters_paid_models_and_includes_future_and_older_models(self) -> None:
        live = self._sample_live_models()

        def fake_gen(model: str, **_kwargs):
            if model in ("gemini-4-argon", "gemini-4-carbon"):
                raise RuntimeError(
                    f"429 RESOURCE_EXHAUSTED: free_tier_requests limit: 0 quotaValue: '0' model: {model}"
                )
            return SimpleNamespace(text="ok")

        fake_client = SimpleNamespace(
            models=SimpleNamespace(list=lambda: iter(live), generate_content=fake_gen)
        )

        with patch.dict(sys.modules, _make_stub_google_genai(), clear=False):
            cat = fetch_dynamic_model_catalog(fake_client, "AIza-free-key", self.cache_path)

        ids = [m["id"] for m in cat["models"]]
        self.assertEqual(cat["key_tier"], "free")
        self.assertEqual(cat["notice"], FREE_KEY_NOTICE)
        self.assertIn("gemini-5-nova", ids)
        self.assertIn("gemini-4-hydrogen", ids)
        self.assertIn("gemini-3.8-flash", ids)
        self.assertIn("gemini-3.1-pro", ids)
        self.assertIn("gemini-2.5-flash", ids)
        # Paid-only, duplicate snapshot, non-chat modalities excluded
        self.assertNotIn("gemini-4-argon", ids)
        self.assertNotIn("gemini-4-carbon", ids)
        self.assertNotIn("gemini-2.5-flash-001", ids)
        self.assertNotIn("gemini-3.8-flash-tts", ids)
        self.assertNotIn("gemini-omni-1.2-flash", ids)
        self.assertNotIn("gemini-embedding-2", ids)
        # Context limit read dynamically from live metadata
        pro_entry = next(m for m in cat["models"] if m["id"] == "gemini-3.1-pro")
        self.assertEqual(pro_entry["context_limit"], 2097152)

    def test_paid_tier_key_exposes_all_chat_models_without_notice(self) -> None:
        live = self._sample_live_models()
        fake_client = SimpleNamespace(
            models=SimpleNamespace(
                list=lambda: iter(live),
                generate_content=lambda **_kw: SimpleNamespace(text="ok"),
            )
        )
        with patch.dict(sys.modules, _make_stub_google_genai(), clear=False):
            cat = fetch_dynamic_model_catalog(fake_client, "AIza-paid-key", self.cache_path)

        ids = [m["id"] for m in cat["models"]]
        self.assertEqual(cat["key_tier"], "paid")
        self.assertIsNone(cat["notice"])
        self.assertIn("gemini-4-argon", ids)
        self.assertIn("gemini-4-carbon", ids)
        self.assertIn("gemini-3.8-flash", ids)
        self.assertIn("gemini-3.1-pro", ids)

    def test_runtime_error_updates_cache_to_free_paid_only(self) -> None:
        live = [
            SimpleNamespace(name="models/gemini-4-hydrogen", display_name="Gemini 4 Hydrogen", supported_actions=["generateContent"], input_token_limit=1048576),
            SimpleNamespace(name="models/gemini-4-carbon", display_name="Gemini 4 Carbon", supported_actions=["generateContent"], input_token_limit=1048576),
        ]
        fake_client = SimpleNamespace(
            models=SimpleNamespace(
                list=lambda: iter(live),
                generate_content=lambda **_kw: SimpleNamespace(text="ok"),
            )
        )
        with patch.dict(sys.modules, _make_stub_google_genai(), clear=False):
            fetch_dynamic_model_catalog(fake_client, "AIza-demo", self.cache_path)
            record_model_runtime_error(
                self.cache_path,
                "AIza-demo",
                "gemini-4-carbon",
                "429 RESOURCE_EXHAUSTED FreeTier quotaValue: '0'",
            )
            cat2 = fetch_dynamic_model_catalog(fake_client, "AIza-demo", self.cache_path)

        self.assertEqual(cat2["key_tier"], "free")
        self.assertEqual(cat2["notice"], FREE_KEY_NOTICE)
        self.assertEqual([m["id"] for m in cat2["models"]], ["gemini-4-hydrogen"])

    def test_non_blocking_probe_false_returns_immediately(self) -> None:
        live = self._sample_live_models()
        gen_calls: list[str] = []

        def fake_gen(model: str, **_kwargs):
            gen_calls.append(model)
            return SimpleNamespace(text="ok")

        fake_client = SimpleNamespace(
            models=SimpleNamespace(list=lambda: iter(live), generate_content=fake_gen)
        )
        with patch.dict(sys.modules, _make_stub_google_genai(), clear=False):
            fast = fetch_dynamic_model_catalog(
                fake_client, "AIza-fast", self.cache_path, probe=False
            )
            self.assertTrue(fast["probing_needed"])
            self.assertEqual(gen_calls, [])
            self.assertGreater(len(fast["models"]), 0)

            probed = fetch_dynamic_model_catalog(
                fake_client, "AIza-fast", self.cache_path, probe=True
            )
            self.assertFalse(probed["probing_needed"])
            self.assertGreater(len(gen_calls), 0)


class TestSearchGatePreviewAndPrompt(unittest.TestCase):
    def test_search_code_gate_preview_shows_glob_and_prompt_rules(self) -> None:
        stubs = _make_stub_google_genai()
        pydantic_mod = pytypes.ModuleType("pydantic")

        class _BaseModel:
            def __init__(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)

        pydantic_mod.BaseModel = _BaseModel
        pydantic_mod.Field = lambda default=None, default_factory=None, **_kw: (
            default_factory() if default_factory else default
        )
        stubs["pydantic"] = pydantic_mod

        saved = {k: sys.modules.get(k) for k in ("models", "services.gemini_client", "services.bridge")}
        try:
            with patch.dict(sys.modules, stubs, clear=False), tempfile.TemporaryDirectory() as tmp:
                for k in ("models", "services.gemini_client", "services.bridge"):
                    sys.modules.pop(k, None)
                (Path(tmp) / "core").mkdir()
                (Path(tmp) / "core" / "a.py").write_text("Surveillance = 1\n", encoding="utf-8")

                gemini_client = importlib.import_module("services.gemini_client")
                proj = SimpleNamespace(root_path=tmp, ignore_patterns=[])
                preview = gemini_client.prepare_tool_gate_preview(
                    proj,
                    "search_code",
                    {"pattern": "Surveillance", "path_glob": "core/**/*.py", "is_regex": False},
                )
                self.assertIn("core/**/*.py", preview["title"])
                self.assertIn("core/**/*.py", preview["detail"])

                prompt_txt = gemini_client.BASE_SYSTEM_PROMPT
                self.assertIn("Need details before we proceed?", prompt_txt)
                self.assertIn("Directives", prompt_txt)
                self.assertIn("propose_script", prompt_txt)
                self.assertIn("PATCH the existing script", prompt_txt)

                prompts_dir = COMPANION_ROOT / "static" / "prompts"
                for fname in ("system_prompt.txt", "bridge_tool_contract.txt", "git_commit_prompt.txt"):
                    self.assertTrue((prompts_dir / fname).is_file(), f"Missing external prompt: {fname}")

                # Verify editing external prompt files takes effect dynamically without module reload
                bridge = importlib.import_module("services.bridge")
                custom_prompts = Path(tmp) / "prompts"
                custom_prompts.mkdir()
                (custom_prompts / "system_prompt.txt").write_text("CUSTOM SYSTEM PROMPT", encoding="utf-8")
                (custom_prompts / "bridge_tool_contract.txt").write_text("CUSTOM BRIDGE CONTRACT", encoding="utf-8")
                (custom_prompts / "git_commit_prompt.txt").write_text("DRAFT:\n{diff}", encoding="utf-8")
                with patch.object(gemini_client, "PROMPTS_DIR", custom_prompts):
                    proj_obj = SimpleNamespace(name="Demo", root_path=tmp, directives_path=None)
                    self.assertIn("CUSTOM SYSTEM PROMPT", gemini_client.load_system_instruction(proj_obj))
                    self.assertEqual(gemini_client.get_git_commit_prompt("diff --git a b"), "DRAFT:\ndiff --git a b")
                    bundle = bridge.build_prompt_bundle(proj_obj, "Hello", [], include_directives=True)
                    self.assertIn("CUSTOM SYSTEM PROMPT", bundle["bundle_text"])
                    self.assertIn("CUSTOM BRIDGE CONTRACT", bundle["bundle_text"])
        finally:
            for k, orig in saved.items():
                if orig is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = orig


class TestSqliteMigration(unittest.TestCase):
    def test_db_migrates_legacy_sessions_table_and_tracks_last_context_tokens(self) -> None:
        if importlib.util.find_spec("pydantic") is None:
            self.skipTest("pydantic not installed in Node container")

        from models import ProjectCreate, SessionCreate, TokenStats
        from storage.db import CompanionDB

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            # Simulate an old schema WITHOUT last_context_tokens column
            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, root_path TEXT NOT NULL,
                    python_path TEXT, directives_path TEXT, backup_dir TEXT NOT NULL DEFAULT 'backup',
                    post_patch_command TEXT, cdp_port INTEGER NOT NULL DEFAULT 9222,
                    ignore_patterns_json TEXT NOT NULL DEFAULT '[]',
                    preset_commands_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
                    model TEXT NOT NULL, thinking_level TEXT NOT NULL DEFAULT 'HIGH',
                    auto_approve_read_tokens INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                """
            )
            conn.commit()
            conn.close()

            db = CompanionDB(db_path)
            proj = db.create_project(ProjectCreate(name="P", root_path=tmp))
            sess = db.create_session(SessionCreate(project_id=proj.id, title="S"))
            db.add_session_tokens(sess.id, TokenStats(input_tokens=4000, output_tokens=100, thinking_tokens=50, total_tokens=4150))
            updated = db.add_session_tokens(sess.id, TokenStats(input_tokens=12000, output_tokens=200, thinking_tokens=100, total_tokens=12300))

            assert updated is not None
            self.assertEqual(updated.token_stats.total_tokens, 16450)
            self.assertEqual(updated.token_stats.last_context_tokens, 12300)


if __name__ == "__main__":
    unittest.main()
