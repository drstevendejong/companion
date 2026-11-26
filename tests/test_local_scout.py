"""Zero-token unit & integration tests for Local AI Scout, extract_headers, rolling command buffer, and Git Scout."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types as pytypes
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))

from config import OUTPUT_BUFFER_MAX_ENTRIES
from services import fs_tools, output_buffer

HAS_WEB_DEPS = all(
    importlib.util.find_spec(mod) is not None
    for mod in ("fastapi", "httpx", "pydantic", "google.genai")
)

if HAS_WEB_DEPS:
    from fastapi.testclient import TestClient
    from app import create_app
    from services.gemini_client import GeminiGateway


_MODULE_STUBS: Dict[str, Any] = {}
_SAVED_MODS: Dict[str, Any] = {}
_TARGET_MODS: tuple[str, ...] = (
    "models",
    "storage.db",
    "services.gemini_client",
    "services.bridge",
    "services.model_catalog",
    "services.local_scout",
    "routes.settings",
    "routes.chat",
    "routes.bridge",
    "routes.git",
    "routes.workspace",
)


def setUpModule() -> None:
    if HAS_WEB_DEPS:
        for name in (
            "models",
            "services.model_catalog",
            "services.local_scout",
            "storage.db",
            "routes.settings",
            "routes.bridge",
            "routes.chat",
            "routes.workspace",
        ):
            _MODULE_STUBS[name] = importlib.import_module(name)
        return

    pydantic_mod = pytypes.ModuleType("pydantic")

    class _BaseModel:
        def __init__(self, **kw: Any) -> None:
            for k, v in kw.items():
                setattr(self, k, v)

        def model_dump(self, **_kw: Any) -> Dict[str, Any]:
            return dict(self.__dict__)

    pydantic_mod.BaseModel = _BaseModel
    pydantic_mod.Field = lambda default=None, default_factory=None, **_kw: (
        default_factory() if default_factory else default
    )

    class _Schema:
        def __init__(self, **_kw: Any) -> None:
            pass

    class _Tool:
        def __init__(self, **_kw: Any) -> None:
            pass

    class _ThinkingConfig:
        def __init__(self, **_kw: Any) -> None:
            self.kwargs = _kw

    class _FunctionDeclaration:
        def __init__(self, **_kw: Any) -> None:
            pass

    class _GenerateContentConfig:
        def __init__(self, **_kw: Any) -> None:
            self.kwargs = _kw

    class _FunctionCall:
        def __init__(self, name: str, args: dict | None = None, id: str | None = None) -> None:
            self.name = name
            self.args = args or {}
            self.id = id

    class _FunctionResponse:
        def __init__(self, name: str, response: dict | None = None, id: str | None = None) -> None:
            self.name = name
            self.response = response or {}
            self.id = id

    class _Part:
        def __init__(
            self,
            text: str | None = None,
            function_call: _FunctionCall | None = None,
            function_response: _FunctionResponse | None = None,
            thought_signature: bytes | None = None,
        ) -> None:
            self.text = text
            self.function_call = function_call
            self.function_response = function_response
            self.thought_signature = thought_signature

        @classmethod
        def from_text(cls, text: str) -> "_Part":
            return cls(text=text)

        @classmethod
        def from_bytes(cls, data: bytes, mime_type: str) -> "_Part":
            return cls(text=f"<{mime_type}:{len(data)}>")

    class _Content:
        def __init__(self, role: str, parts: list) -> None:
            self.role = role
            self.parts = parts

    genai_types = pytypes.ModuleType("google.genai.types")
    genai_types.Schema = _Schema
    genai_types.Tool = _Tool
    genai_types.Type = SimpleNamespace(OBJECT="OBJECT", STRING="STRING", INTEGER="INTEGER", BOOLEAN="BOOLEAN")
    genai_types.ThinkingLevel = SimpleNamespace(MINIMAL="MINIMAL", LOW="LOW", MEDIUM="MEDIUM", HIGH="HIGH", XHIGH="XHIGH")
    genai_types.ThinkingConfig = _ThinkingConfig
    genai_types.FunctionDeclaration = _FunctionDeclaration
    genai_types.GenerateContentConfig = _GenerateContentConfig
    genai_types.FunctionCall = _FunctionCall
    genai_types.FunctionResponse = _FunctionResponse
    genai_types.Part = _Part
    genai_types.Content = _Content

    genai_mod = pytypes.ModuleType("google.genai")
    genai_mod.types = genai_types
    genai_mod.Client = object

    google_mod = pytypes.ModuleType("google")
    google_mod.genai = genai_mod

    class _HTTPException(Exception):
        def __init__(self, status_code: int = 400, detail: str = "") -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class _APIRouter:
        def __init__(self, **_kw: Any) -> None:
            pass

        def get(self, *_a: Any, **_kw: Any):
            return lambda fn: fn

        def post(self, *_a: Any, **_kw: Any):
            return lambda fn: fn

        def patch(self, *_a: Any, **_kw: Any):
            return lambda fn: fn

        def delete(self, *_a: Any, **_kw: Any):
            return lambda fn: fn

    fastapi_mod = pytypes.ModuleType("fastapi")
    fastapi_mod.APIRouter = _APIRouter
    fastapi_mod.HTTPException = _HTTPException
    fastapi_mod.Query = lambda default=None, **_kw: default
    fastapi_mod.Request = object

    stubs = {
        "pydantic": pydantic_mod,
        "google": google_mod,
        "google.genai": genai_mod,
        "google.genai.types": genai_types,
        "fastapi": fastapi_mod,
    }
    all_keys = tuple(stubs.keys()) + _TARGET_MODS
    _SAVED_MODS.update({k: sys.modules.get(k) for k in all_keys})
    for m in _TARGET_MODS:
        sys.modules.pop(m, None)
    sys.modules.update(stubs)

    for name in (
        "models",
        "services.model_catalog",
        "services.local_scout",
        "storage.db",
        "routes.settings",
        "routes.bridge",
        "routes.chat",
        "routes.workspace",
    ):
        _MODULE_STUBS[name] = importlib.import_module(name)


def tearDownModule() -> None:
    if not HAS_WEB_DEPS and _SAVED_MODS:
        for k, prev in _SAVED_MODS.items():
            if prev is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev
        _SAVED_MODS.clear()
    _MODULE_STUBS.clear()


class _StubbedScoutTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.models_mod = _MODULE_STUBS["models"]
        cls.model_catalog = _MODULE_STUBS["services.model_catalog"]
        cls.local_scout = _MODULE_STUBS["services.local_scout"]
        cls.db_mod = _MODULE_STUBS["storage.db"]
        cls.settings_rt = _MODULE_STUBS["routes.settings"]
        cls.bridge_rt = _MODULE_STUBS["routes.bridge"]
        cls.chat_rt = _MODULE_STUBS["routes.chat"]
        cls.workspace_rt = _MODULE_STUBS["routes.workspace"]


def _make_response(
    text: str = "",
    function_calls: List[tuple[str, dict]] | None = None,
    thought_signature: bytes | None = None,
    tokens: tuple[int, int, int] = (100, 25, 10),
) -> Any:
    parts = []
    if text:
        parts.append(SimpleNamespace(text=text, thought=False, function_call=None))
    for idx, (name, args) in enumerate(function_calls or []):
        parts.append(
            SimpleNamespace(
                text=None,
                thought_signature=thought_signature if idx == 0 else None,
                function_call=SimpleNamespace(id=f"call-{idx}", name=name, args=args),
            )
        )
    p_tok, c_tok, t_tok = tokens
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
        usage_metadata=SimpleNamespace(
            prompt_token_count=p_tok,
            candidates_token_count=c_tok,
            thoughts_token_count=t_tok,
            total_token_count=p_tok + c_tok + t_tok,
        ),
    )


class _ScriptedModels:
    def __init__(self, handler_or_turns: Any) -> None:
        self._handler_or_turns = handler_or_turns
        self.calls: List[Dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if callable(self._handler_or_turns):
            return self._handler_or_turns(kwargs, len(self.calls))
        if not self._handler_or_turns:
            raise AssertionError("Unexpected extra generate_content call in test.")
        nxt = self._handler_or_turns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if callable(nxt):
            return nxt(kwargs)
        return nxt


class _FakeGeminiGateway:
    """Lightweight GeminiGateway test double usable directly with local_scout helpers."""

    def __init__(self, client: Any, catalog: Dict[str, Any] | None = None) -> None:
        self._client = client
        self._client_factory = lambda: client
        self.runtime_errors: List[tuple[str, str]] = []
        self._catalog = catalog or {
            "models": [
                {"id": "gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash Lite", "context_limit": 1048576, "tier_status": "usable"},
                {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite", "context_limit": 1048576, "tier_status": "usable"},
                {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "context_limit": 1048576, "tier_status": "usable"},
                {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "context_limit": 1048576, "tier_status": "usable"},
            ],
            "paid_only_models": [],
        }

    def _get_client(self) -> Any:
        return self._client

    def get_model_catalog(self, force_refresh: bool = False, probe: bool = False) -> Dict[str, Any]:
        return self._catalog

    def record_runtime_error(self, model_id: str, exc: Exception) -> None:
        self.runtime_errors.append((model_id, str(exc)))


class TestOutputBuffer(unittest.TestCase):
    def setUp(self) -> None:
        output_buffer.clear_buffer()

    def tearDown(self) -> None:
        output_buffer.clear_buffer()

    def test_empty_and_missing_exec_id_return_clean_error_dict(self) -> None:
        res = output_buffer.read_command_output()
        self.assertFalse(res["found"])
        self.assertIn("No buffered command output", res["error"])

        res2 = output_buffer.read_command_output("deadbeef")
        self.assertFalse(res2["found"])

    def test_slice_tail_pattern_and_rolling_eviction(self) -> None:
        lines = [f"line {i}: ok" if i != 12 else "line 12: ERROR boom_token" for i in range(1, 26)]
        eid = output_buffer.record_command_output("pytest -q", "\n".join(lines), exit_code=1, duration_ms=42)

        r_slice = output_buffer.read_command_output(eid, start_line=10, end_line=14)
        self.assertTrue(r_slice["found"])
        self.assertFalse(r_slice["passed"])
        self.assertEqual(r_slice["mode"], "range")
        self.assertEqual(r_slice["shown_lines"], 5)
        self.assertIn("12: line 12: ERROR boom_token", r_slice["content"])

        r_tail = output_buffer.read_command_output(None, tail_lines=3)
        self.assertEqual(r_tail["exec_id"], eid)
        self.assertEqual(r_tail["mode"], "tail")
        self.assertEqual(r_tail["shown_lines"], 3)
        self.assertIn("25: line 25: ok", r_tail["content"])

        r_pat = output_buffer.read_command_output(eid, pattern="boom_token", context_lines=1)
        self.assertEqual(r_pat["mode"], "pattern")
        self.assertEqual(r_pat["match_count"], 1)
        self.assertIn("11: line 11: ok", r_pat["content"])
        self.assertIn("12: line 12: ERROR boom_token", r_pat["content"])

        for idx in range(OUTPUT_BUFFER_MAX_ENTRIES + 3):
            output_buffer.record_command_output(f"cmd-{idx}", f"out-{idx}")
        evicted = output_buffer.read_command_output(eid)
        self.assertFalse(evicted["found"])


class TestExtractHeaders(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "node_modules").mkdir()
        (self.root / "src" / "auth.py").write_text(
            '"""Authentication helpers and JWT checks."""\n\nclass AuthService:\n    def verify(self, token):\n        return True\n\ndef hash_pw(raw):\n    return raw\n',
            encoding="utf-8",
        )
        (self.root / "src" / "app.js").write_text(
            "/** Client bootstrap utilities */\nexport async function initApp() {}\nclass Widget {}\n",
            encoding="utf-8",
        )
        (self.root / "src" / "page.html").write_text(
            "<!-- Main dashboard template -->\n<html><body>Hi</body></html>\n",
            encoding="utf-8",
        )
        (self.root / "node_modules" / "ignored.js").write_text("// should be ignored\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_extracts_headers_and_symbols_with_pattern_and_ignores(self) -> None:
        res_all = fs_tools.extract_headers(self.root)
        self.assertEqual(res_all["file_count"], 3)
        self.assertEqual(res_all["header_count"], 3)
        paths = {it["path"] for it in res_all["items"]}
        self.assertEqual(paths, {"src/auth.py", "src/app.js", "src/page.html"})
        self.assertIn("Authentication helpers and JWT checks.", res_all["formatted"])
        self.assertIn("class AuthService(verify)", res_all["formatted"])
        self.assertIn("function initApp", res_all["formatted"])
        self.assertIn("Main dashboard template", res_all["formatted"])

        res_py = fs_tools.extract_headers(self.root, pattern="*.py")
        self.assertEqual(res_py["file_count"], 1)
        self.assertEqual(res_py["items"][0]["path"], "src/auth.py")


class TestModelCatalogLiteRanking(_StubbedScoutTestCase):
    def test_ranks_newest_usable_lite_and_never_dips_into_primary_flash_or_pro(self) -> None:
        catalog = {
            "models": [
                {"id": "gemini-2.5-pro", "tier_status": "usable"},
                {"id": "gemini-2.5-flash", "tier_status": "usable"},
                {"id": "gemini-2.5-flash-lite", "tier_status": "usable"},
                {"id": "gemini-3.1-flash-lite", "tier_status": "usable"},
                {"id": "gemini-3.5-flash-lite", "tier_status": "paid_only"},
                {"id": "gemini-2.0-flash-lite", "tier_status": "unavailable"},
            ],
            "paid_only_models": ["gemini-3.5-flash-lite"],
        }
        ranked = self.model_catalog.rank_light_scout_models(catalog, allow_non_lite_fallback=False)
        self.assertEqual(ranked, ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite"])

        now = time.time()
        cooling = {"gemini-3.1-flash-lite": now + 60, "gemini-2.5-flash-lite": now + 60}
        ranked_empty = self.model_catalog.rank_light_scout_models(
            catalog, cooldowns=cooling, now_ts=now, allow_non_lite_fallback=False
        )
        self.assertEqual(ranked_empty, [])


class TestLocalScoutLoop(_StubbedScoutTestCase):
    def setUp(self) -> None:
        self.local_scout.clear_cooldowns()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()
        (self.root / "core.py").write_text(
            '"""Core payment processor module."""\n\ndef charge_card(amount):\n    return amount * 100\n',
            encoding="utf-8",
        )
        (self.root / "fail_mod.py").write_text(
            'def compute_ratio(x, y):\n    return x / y\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.local_scout.clear_cooldowns()
        self.tmp.cleanup()

    def _make_project(self) -> Any:
        return self.models_mod.ProjectProfile(
            id="p1",
            name="ScoutProj",
            root_path=str(self.root),
            backup_dir="backup",
            ignore_patterns=[],
            test_command="pytest",
        )

    def test_multi_step_stateless_loop_and_mid_loop_model_hop(self) -> None:
        project = self._make_project()
        turns = [
            _make_response(
                text="",
                function_calls=[("extract_headers", {"subpath": "."})],
                thought_signature=b"sig-1",
            ),
            RuntimeError("429 ResourceExhausted quota exceeded"),
            _make_response(
                text="",
                function_calls=[("search_code", {"pattern": "charge_card"})],
                thought_signature=b"sig-2",
            ),
            _make_response(text="Found `charge_card` in `core.py:3` multiplying amount by 100."),
        ]
        models = _ScriptedModels(turns)
        gw = _FakeGeminiGateway(SimpleNamespace(models=models))

        res = self.local_scout.run_scout_workspace(
            gw,
            None,
            project,
            session_id="s1",
            goal="Locate charge_card logic",
            max_steps=6,
            max_tokens=30000,
        )
        self.assertEqual(res["model_used"], "gemini-2.5-flash-lite")
        self.assertIn("gemini-3.1-flash-lite -> gemini-2.5-flash-lite", res["models_hopped"])
        self.assertEqual(res["steps_taken"], 2)
        self.assertIn("core.py:3", res["answer"])
        self.assertFalse(res["stopped_early"])

    def test_pause_resume_and_early_stop_synthesizes_partial_findings(self) -> None:
        project = self._make_project()
        rid = "run-stop-test"

        def handler(kwargs: Dict[str, Any], call_no: int) -> Any:
            if call_no == 1:
                return _make_response(function_calls=[("search_code", {"pattern": "charge_card"})])
            if call_no == 2:
                ctrl = self.local_scout.get_scout_run(rid)
                self.assertIsNotNone(ctrl)
                ctrl.pause()

                def _stop_soon() -> None:
                    time.sleep(0.05)
                    ctrl.stop()

                threading.Thread(target=_stop_soon, daemon=True).start()
                time.sleep(0.01)
                return _make_response(function_calls=[("read_lines", {"path": "core.py", "start_line": 1, "end_line": 4})])
            return _make_response(text="Observed `charge_card` in `core.py:3`.")

        gw = _FakeGeminiGateway(SimpleNamespace(models=_ScriptedModels(handler)))
        res = self.local_scout.run_scout_workspace(
            gw,
            None,
            project,
            session_id="s1",
            goal="Inspect charge_card",
            run_id=rid,
        )
        self.assertTrue(res["stopped_early"])
        self.assertTrue(res["answer"].startswith("[Stopped early by user]"))
        self.assertIn("core.py:3", res["answer"])

    def test_token_budget_cap_triggers_synthesis(self) -> None:
        project = self._make_project()
        turns = [
            _make_response(
                function_calls=[("search_code", {"pattern": "charge_card"})],
                tokens=(3500, 500, 200),
            ),
            _make_response(text="Partial synthesis: charge_card is at core.py:3."),
        ]
        gw = _FakeGeminiGateway(SimpleNamespace(models=_ScriptedModels(turns)))
        res = self.local_scout.run_scout_workspace(
            gw,
            None,
            project,
            session_id="s1",
            goal="Inspect",
            max_tokens=4000,
        )
        self.assertIn("[Budget cap reached", res["answer"])
        self.assertIn("core.py:3", res["answer"])

    def test_summarize_triage_headers_and_git_draft(self) -> None:
        project = self._make_project()
        self.assertIsNone(
            self.local_scout.summarize_command_output(
                _FakeGeminiGateway(SimpleNamespace(models=_ScriptedModels([]))),
                None,
                command="pytest",
                raw_output="2 passed",
                exit_code=0,
            )
        )

        gw_sum = _FakeGeminiGateway(SimpleNamespace(models=_ScriptedModels([
            _make_response(text="Yes, CoreApp exists on line 2.")
        ])))
        s_res = self.local_scout.summarize_command_output(
            gw_sum,
            None,
            command="pytest -v",
            raw_output="CoreApp passed\nOtherTest passed",
            exit_code=0,
            focus="Did CoreApp pass?",
        )
        self.assertIsNotNone(s_res)
        self.assertIn("CoreApp", s_res["summary"])

        gw_tri = _FakeGeminiGateway(SimpleNamespace(models=_ScriptedModels([
            _make_response(function_calls=[("read_lines", {"path": "fail_mod.py", "start_line": 1, "end_line": 2})]),
            _make_response(text="ZeroDivisionError at fail_mod.py:2 because y=0."),
        ])))
        t_res = self.local_scout.triage_command_failure(
            gw_tri,
            None,
            project,
            command="pytest",
            raw_output="Traceback: fail_mod.py:2 ZeroDivisionError",
            exit_code=1,
        )
        self.assertIsNotNone(t_res)
        self.assertIn("fail_mod.py:2", t_res["summary"])

        before = (self.root / "fail_mod.py").read_text(encoding="utf-8")
        gw_hdr = _FakeGeminiGateway(SimpleNamespace(models=_ScriptedModels([
            _make_response(text='"""Ratio arithmetic helper module."""')
        ])))
        hdr_res = self.local_scout.draft_header_updates(gw_hdr, None, project, paths=["fail_mod.py"])
        self.assertEqual(len(hdr_res["proposed_patches"]), 1)
        self.assertIn("Ratio arithmetic helper module.", hdr_res["proposed_patches"][0]["diff"])
        self.assertEqual((self.root / "fail_mod.py").read_text(encoding="utf-8"), before)

        gw_git = _FakeGeminiGateway(SimpleNamespace(models=_ScriptedModels([
            _make_response(text="fix: guard division by zero in fail_mod")
        ])))
        git_draft = self.local_scout.draft_git_commit_with_scout(
            gw_git, None, "diff --git a/fail_mod.py b/fail_mod.py\n+if y == 0:"
        )
        self.assertEqual(git_draft["message"], "fix: guard division by zero in fail_mod")

    def test_scout_settings_bridge_notice_and_header_gate_route_handlers(self) -> None:
        """Exercises routes.settings, routes.bridge, and routes.chat logic directly even without fastapi installed."""
        db_path = Path(self.tmp.name) / "scout_routes.db"
        db = self.db_mod.CompanionDB(db_path)
        proj = db.create_project(self.models_mod.ProjectCreate(name="Repo", root_path=str(self.root)))
        sess = db.create_session(self.models_mod.SessionCreate(project_id=proj.id, title="Bridge", api_enabled=False))

        models = _ScriptedModels([
            _make_response(text='"""Updated payment processor header."""'),
        ])
        gw = _FakeGeminiGateway(SimpleNamespace(models=models))
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=gw, event_loop=None)))

        no_key_req = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=SimpleNamespace(key_path=Path(self.tmp.name) / "missing.key")))
        )
        with self.assertRaises(Exception):
            self.settings_rt.update_scout_settings_endpoint(self.models_mod.ScoutSettingsUpdate(enabled=True), no_key_req)

        st = self.settings_rt.update_scout_settings_endpoint(self.models_mod.ScoutSettingsUpdate(enabled=True, auto_approve=False), req)
        self.assertTrue(st["enabled"])

        b1 = self.bridge_rt.create_bridge_bundle(
            self.models_mod.BridgeBundleRequest(session_id=sess.id, prompt="Hi", include_directives=False, record_turn=True),
            req,
        )
        self.assertIn("[COMPANION NOTICE]", b1["bundle_text"])
        self.assertIn("Local AI Scout is NOW ENABLED", b1["bundle_text"])

        b2 = self.bridge_rt.create_bridge_bundle(
            self.models_mod.BridgeBundleRequest(session_id=sess.id, prompt="Next", include_directives=False, record_turn=True),
            req,
        )
        self.assertNotIn("[COMPANION NOTICE]", b2["bundle_text"])

        self.settings_rt.update_scout_settings_endpoint(self.models_mod.ScoutSettingsUpdate(enabled=False), req)
        p_off = asyncio.run(
            self.bridge_rt.parse_bridge_response(
                self.models_mod.BridgeParseRequest(
                    session_id=sess.id,
                    raw_markdown='```json\n{"tool": "scout_workspace", "goal": "find charge"}\n```',
                ),
                req,
            )
        )
        self.assertEqual(p_off["error_count"], 1)
        self.assertIn("Local AI Scout is currently OFF", p_off["error_reply_text"])
        self.assertIn("Local AI Scout is NOW OFF", p_off["bridge_reply_text"])

        self.settings_rt.update_scout_settings_endpoint(self.models_mod.ScoutSettingsUpdate(enabled=True), req)
        p_hdr = asyncio.run(
            self.bridge_rt.parse_bridge_response(
                self.models_mod.BridgeParseRequest(
                    session_id=sess.id,
                    raw_markdown='```json\n{"tool": "update_header", "paths": ["core.py"]}\n```',
                ),
                req,
            )
        )
        self.assertEqual(len(p_hdr["pending_gates"]), 1)
        hdr_gate = p_hdr["pending_gates"][0]
        self.assertEqual(hdr_gate["tool_name"], "update_header")

        before_disk = (self.root / "core.py").read_text(encoding="utf-8")
        asyncio.run(
            self.chat_rt._execute_gate_decision(
                db,
                hdr_gate,
                self.models_mod.ToolGateDecision(action="approve"),
                db.get_session(sess.id),
                proj,
                app=req.app,
                gemini=gw,
            )
        )
        self.assertEqual((self.root / "core.py").read_text(encoding="utf-8"), before_disk)
        staged_writes = [g for g in db.list_pending_gates(sess.id) if g["tool_name"] == "write_file"]
        self.assertEqual(len(staged_writes), 1)
        self.assertIn("Updated payment processor header.", staged_writes[0]["preview"]["diff"])

    def test_selective_failure_triage_and_lowest_thinking_levels(self) -> None:
        """Short failures skip Scout triage; noisy failures (>1500 chars) or focus trigger MINIMAL triage; bridge omits duplicate preview."""
        project = self._make_project()
        scout_cfg = {"enabled": True, "summarize_threshold_chars": 4000}

        # 1. Short 4-line failure without focus -> 0 local Scout calls!
        models_unused = _ScriptedModels([])
        gw_unused = _FakeGeminiGateway(SimpleNamespace(models=models_unused))
        short_res = asyncio.run(
            self.chat_rt._maybe_summarize_or_triage_output(
                gemini=gw_unused,
                db=None,
                project=project,
                scout_cfg=scout_cfg,
                command="python probe.py",
                raw_output="Traceback (most recent call last):\n  File 'probe.py', line 12\nStopIteration",
                exit_code=1,
                passed=False,
                focus=None,
                exec_id="e1",
            )
        )
        self.assertEqual(short_res, {})
        self.assertEqual(len(models_unused.calls), 0)

        # 2. Long noisy failure (>1500 chars) -> runs MINIMAL thinking triage; fallback to LOW if MINIMAL 400s
        noisy_fail = ("ok line\n" * 260) + "AssertionError: expected 2 got 3 at fail_mod.py:2"
        models_triage = _ScriptedModels([
            RuntimeError("400 Thinking level MINIMAL is not supported"),
            _make_response(text="- `fail_mod.py:2`: `AssertionError: expected 2 got 3`"),
        ])
        gw_triage = _FakeGeminiGateway(SimpleNamespace(models=models_triage))
        long_res = asyncio.run(
            self.chat_rt._maybe_summarize_or_triage_output(
                gemini=gw_triage,
                db=None,
                project=project,
                scout_cfg=scout_cfg,
                command="pytest",
                raw_output=noisy_fail,
                exit_code=1,
                passed=False,
                focus=None,
                exec_id="e2",
            )
        )
        self.assertIn("AssertionError", long_res["summary"])
        # First attempt requested MINIMAL (or budget 0 on 2.5); retry dropped to LOW on same model
        first_cfg = models_triage.calls[0]["config"].kwargs.get("thinking_config")
        retry_cfg = models_triage.calls[1]["config"].kwargs.get("thinking_config")
        if not HAS_WEB_DEPS:
            self.assertEqual(first_cfg.kwargs.get("thinking_level"), "MINIMAL")
            self.assertEqual(retry_cfg.kwargs.get("thinking_level"), "LOW")

        # 3. Gemini 2.5 MINIMAL uses thinking_budget=0, whereas scout_workspace uses LOW
        cfg_25 = self.local_scout._resolve_thinking_config("gemini-2.5-flash-lite", "MINIMAL")
        self.assertIsNotNone(cfg_25)
        if not HAS_WEB_DEPS:
            self.assertEqual(cfg_25.kwargs.get("thinking_budget"), 0)

        # 4. Bridge formatter omits duplicate raw block cleanly when output_preview is empty + ai_summary + exec_id set
        bridge_svc = importlib.import_module("services.bridge")
        rendered = bridge_svc.format_bridge_tool_results([
            {
                "tool_name": "run_tests",
                "response": {
                    "command": "pytest",
                    "exec_id": "abc12345",
                    "exit_code": 1,
                    "passed": False,
                    "has_traceback": False,
                    "output_preview": "",
                    "ai_summary": "- `fail_mod.py:2`: `AssertionError`",
                },
            }
        ])
        self.assertIn("> **Scout Summary:** - `fail_mod.py:2`: `AssertionError`", rendered)
        self.assertIn("exec_id=abc12345", rendered)
        self.assertIn("read_command_output", rendered)
        self.assertNotIn("```text", rendered)

    def test_scout_per_model_telemetry_and_bridge_mode(self) -> None:
        """Scout telemetry tracks per-model RPM/TPM/Tokens across hops and updates in Bridge mode."""
        from datetime import datetime, timezone

        db_path = Path(self.tmp.name) / "scout_tel.db"
        db = self.db_mod.CompanionDB(db_path)
        proj = db.create_project(self.models_mod.ProjectCreate(name="Repo", root_path=str(self.root)))
        sess = db.create_session(self.models_mod.SessionCreate(project_id=proj.id, title="Bridge", api_enabled=False))

        now_ts = datetime(2026, 11, 21, 18, 0, 0, tzinfo=timezone.utc).timestamp()
        db.record_api_usage(
            "gemini-4-hydrogen",
            self.models_mod.TokenStats(input_tokens=5000, output_tokens=200, thinking_tokens=100),
            created_ts=now_ts - 10,
            source="main",
        )
        db.record_api_usage(
            "gemini-3.1-flash-lite",
            self.models_mod.TokenStats(input_tokens=800, output_tokens=120, thinking_tokens=30),
            created_ts=now_ts - 5,
            source="scout",
        )
        db.record_api_usage(
            "gemini-2.5-flash-lite",
            self.models_mod.TokenStats(input_tokens=400, output_tokens=60, thinking_tokens=0),
            created_ts=now_ts - 2,
            source="scout",
        )

        snap = db.get_rate_telemetry(
            model="gemini-4-hydrogen",
            now_ts=now_ts,
            scout_models=["gemini-3.1-flash-lite", "gemini-2.5-flash-lite"],
        )
        # Main AI stats stay scoped to Main model
        self.assertEqual(snap["rpm"], 1)
        self.assertEqual(snap["tpm"], 5000)
        # Scout aggregate & per-model breakdown stay separate and sum across Scout models
        sc = snap["scout"]
        self.assertEqual(sc["active_model"], "gemini-3.1-flash-lite")
        self.assertEqual(sc["rpm"], 2)
        self.assertEqual(sc["tpm"], 1200)
        self.assertEqual(sc["rpd"], 2)
        self.assertEqual(sc["total_tokens"], 950 + 460)
        by_m = {m["model"]: m for m in sc["models"]}
        self.assertEqual(by_m["gemini-3.1-flash-lite"]["total_tokens"], 950)
        self.assertEqual(by_m["gemini-3.1-flash-lite"]["rpm"], 1)
        self.assertEqual(by_m["gemini-3.1-flash-lite"]["tpm"], 800)
        self.assertEqual(by_m["gemini-2.5-flash-lite"]["total_tokens"], 460)
        self.assertEqual(by_m["gemini-2.5-flash-lite"]["rpm"], 1)
        self.assertEqual(by_m["gemini-2.5-flash-lite"]["tpm"], 400)

        # Bridge auto-approved scout_workspace returns live telemetry including Scout tokens
        models = _ScriptedModels([
            _make_response(text="Found it in core.py.", tokens=(310, 45, 10)),
        ])
        gw = _FakeGeminiGateway(SimpleNamespace(models=models))
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=gw, event_loop=None)))
        self.settings_rt.update_scout_settings_endpoint(
            self.models_mod.ScoutSettingsUpdate(enabled=True, auto_approve=True), req
        )
        parsed = asyncio.run(
            self.bridge_rt.parse_bridge_response(
                self.models_mod.BridgeParseRequest(
                    session_id=sess.id,
                    raw_markdown='```json\n{"tool": "scout_workspace", "goal": "find charge"}\n```',
                ),
                req,
            )
        )
        self.assertIn("telemetry", parsed)
        self.assertGreaterEqual(parsed["telemetry"]["scout"]["rpd"], 1)
        self.assertGreaterEqual(parsed["telemetry"]["scout"]["total_tokens"], 365)

    def test_script_failure_classification_and_retry_script_route(self) -> None:
        """propose_script defaults delete_on_success=True; runtime errors are retryable while static errors are not."""
        gemini_client = importlib.import_module("services.gemini_client")

        db_path = Path(self.tmp.name) / "script_retry.db"
        db = self.db_mod.CompanionDB(db_path)
        proj = db.create_project(self.models_mod.ProjectCreate(name="Repo", root_path=str(self.root)))
        sess = db.create_session(self.models_mod.SessionCreate(project_id=proj.id, title="Bridge", api_enabled=False))
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=None, event_loop=None)))

        # 1. propose_script preview defaults delete_on_success=True
        prev = gemini_client.prepare_tool_gate_preview(
            proj, "propose_script", {"path": "tmp_probe.py", "content": "print('hi')\n"}
        )
        self.assertTrue(prev["delete_on_success"])

        # 2. Failure classifier distinguishes static vs runtime errors
        self.assertEqual(
            self.chat_rt.classify_script_failure("SyntaxError: invalid syntax", 1, "def broken(:\n"),
            "static",
        )
        self.assertEqual(
            self.chat_rt.classify_script_failure("Traceback:\nNameError: name 'x' is not defined\n", 1, "print(x)\n"),
            "static",
        )
        self.assertEqual(
            self.chat_rt.classify_script_failure(
                "urllib.error.URLError: <urlopen error [WinError 10061] No connection could be made>\n",
                1,
                "print('ok')\n",
            ),
            "runtime",
        )

        # 3. Runtime-failing temporary script stays on disk with retryable=True; Retry succeeds -> auto-deletes & replaces trailing tool_event
        flag_file = self.root / "chrome_ready.txt"
        script_code = (
            "from pathlib import Path\n"
            "import sys\n"
            "if not Path('chrome_ready.txt').exists():\n"
            "    raise ConnectionRefusedError('Chrome :9222 not open')\n"
            "print('PROBE_CONNECTED_OK')\n"
        )
        gate = db.create_gate(
            sess.id,
            "propose_script",
            {"path": "probe_cdp.py", "content": script_code, "summary": "Probe CDP"},
            {"summary": "Probe CDP", "delete_on_success": True},
        )
        fail_res = asyncio.run(
            self.chat_rt.resolve_tool_gate(gate["id"], SimpleNamespace(action="write_run_and_delete", reason=None), req)
        )
        self.assertTrue((self.root / "probe_cdp.py").is_file())
        self.assertIn("ConnectionRefusedError", fail_res["bridge_reply_text"])
        ev_fail = json.loads(db.list_messages(sess.id)[-1].text)["response"]
        self.assertEqual(ev_fail["failure_kind"], "runtime")
        self.assertTrue(ev_fail["retryable"])
        self.assertTrue(ev_fail["auto_delete_on_success"])

        # Open "Chrome :9222" and retry script
        flag_file.write_text("ready", encoding="utf-8")
        retry_res = asyncio.run(
            self.chat_rt.retry_failed_script(
                self.models_mod.RetryScriptRequest(
                    session_id=sess.id,
                    path="probe_cdp.py",
                    auto_delete=True,
                    tool_name="propose_script",
                ),
                req,
            )
        )
        self.assertFalse((self.root / "probe_cdp.py").exists(), "Passing retry must auto-delete temporary script")
        self.assertIn("PROBE_CONNECTED_OK", retry_res["bridge_reply_text"])
        self.assertNotIn("ConnectionRefusedError", retry_res["bridge_reply_text"])
        self.assertEqual(db.list_checkpoints(sess.id), [], "Auto-deleted temp script with no other changes should not keep a Savepoint")

        # 4. Static syntax error script cannot be retried
        (self.root / "broken_static.py").write_text("def oops(:\n    pass\n", encoding="utf-8")
        with self.assertRaises(Exception):
            asyncio.run(
                self.chat_rt.retry_failed_script(
                    self.models_mod.RetryScriptRequest(session_id=sess.id, path="broken_static.py"),
                    req,
                )
            )

    def test_scout_emits_phase_direct_synthesis_and_guaranteed_done(self) -> None:
        db_path = Path(self.tmp.name) / "scout_events.db"
        db = self.db_mod.CompanionDB(db_path)
        proj = db.create_project(self.models_mod.ProjectCreate(name="Repo", root_path=str(self.root)))
        sess = db.create_session(self.models_mod.SessionCreate(project_id=proj.id, title="Bridge", api_enabled=False))

        emitted: List[Dict[str, Any]] = []

        def _capture_emit(app, loop, payload):
            emitted.append(payload)

        # Direct text response on step 1 (no function calls) still emits phase, synthesis step, and done
        models_direct = _ScriptedModels([_make_response(text="Direct synthesis answer.", tokens=(150, 25, 5))])
        gw = _FakeGeminiGateway(SimpleNamespace(models=models_direct))
        with patch.object(self.local_scout, "_emit_ws", side_effect=_capture_emit):
            res = self.local_scout.run_scout_workspace(
                gw,
                db,
                proj,
                session_id=sess.id,
                goal="Find where auth happens",
                subpath=".",
                max_steps=4,
            )
        self.assertEqual(res["answer"], "Direct synthesis answer.")
        types_seen = [e["type"] for e in emitted]
        self.assertIn("scout_start", types_seen)
        self.assertIn("scout_phase", types_seen)
        self.assertIn("scout_step", types_seen)
        self.assertEqual(types_seen[-1], "scout_done")

        # Even if generation throws, scout_done is guaranteed in finally
        emitted.clear()
        class _BoomModels:
            def generate_content(self, **_kw):
                raise RuntimeError("boom error")
        gw_boom = _FakeGeminiGateway(SimpleNamespace(models=_BoomModels()))
        with patch.object(self.local_scout, "_emit_ws", side_effect=_capture_emit):
            with self.assertRaises(RuntimeError):
                self.local_scout.run_scout_workspace(
                    gw_boom,
                    db,
                    proj,
                    session_id=sess.id,
                    goal="Fail test",
                    subpath=".",
                    max_steps=2,
                )
        self.assertEqual(emitted[-1]["type"], "scout_done")

    def test_command_summarization_and_triage_emit_exec_phase(self) -> None:
        db_path = Path(self.tmp.name) / "exec_phase.db"
        db = self.db_mod.CompanionDB(db_path)
        proj = db.create_project(
            self.models_mod.ProjectCreate(
                name="PhaseRepo",
                root_path=str(self.root),
                post_patch_command='python3 -c "print(\'PASS_LINE \' * 400)"',
            )
        )
        sess = db.create_session(self.models_mod.SessionCreate(project_id=proj.id, title="Bridge", api_enabled=False))

        ws_events: List[Dict[str, Any]] = []

        class _FakeWS:
            async def send_text(self, payload: str) -> None:
                ws_events.append(json.loads(payload))

        app = SimpleNamespace(state=SimpleNamespace(db=db, gemini=None, lifecycle_sockets={_FakeWS()}))
        req = SimpleNamespace(app=app)
        gw = _FakeGeminiGateway(SimpleNamespace(models=_ScriptedModels([])))
        app.state.gemini = gw

        # 1. Passing run_tests gate with Scout summarization emits exec_done -> exec_phase(scout_summarize) -> exec_phase(done)
        gate = db.create_gate(
            sess.id,
            "run_tests",
            {"summary": "Run verbose tests"},
            {"summary": "Run verbose tests", "command": proj.post_patch_command},
        )
        with (
            patch.object(self.chat_rt, "_get_effective_scout_settings", return_value={"enabled": True, "summarize_threshold_chars": 500}),
            patch.object(
                self.local_scout,
                "summarize_command_output",
                return_value={"summary": "1 test batch passed cleanly.", "model_used": "gemini-3.1-flash-lite"},
            ),
        ):
            res = asyncio.run(
                self.chat_rt.resolve_tool_gate(
                    gate["id"],
                    SimpleNamespace(action="approve", reason=None),
                    req,
                )
            )
        self.assertIn("1 test batch passed cleanly.", res["bridge_reply_text"])
        phase_events = [e for e in ws_events if e.get("type") == "exec_phase"]
        self.assertEqual([e.get("phase") for e in phase_events], ["scout_summarize", "done"])
        self.assertIn("Scout is summarizing", phase_events[0]["status_text"])
        self.assertEqual(phase_events[1]["status_text"], "")
        event_types = [e.get("type") for e in ws_events]
        self.assertIn("exec_done", event_types)
        self.assertLess(event_types.index("exec_done"), event_types.index("exec_phase"))

        # 2. Failed output triage emits exec_phase(scout_triage) -> exec_phase(done) even when triage raises
        ws_events.clear()
        with patch.object(self.local_scout, "triage_command_failure", side_effect=RuntimeError("network blip")):
            triage_res = asyncio.run(
                self.chat_rt._maybe_summarize_or_triage_output(
                    gemini=gw,
                    db=db,
                    project=proj,
                    scout_cfg={"enabled": True, "summarize_threshold_chars": 500},
                    command="pytest",
                    raw_output="E AssertionError: failed\n" * 80,
                    exit_code=1,
                    passed=False,
                    focus=None,
                    exec_id="exec-1",
                    app=app,
                    session_id=sess.id,
                    gate_id="g-1",
                    run_id="r-1",
                )
            )
        self.assertEqual(triage_res, {})
        triage_phases = [e for e in ws_events if e.get("type") == "exec_phase"]
        self.assertEqual([e.get("phase") for e in triage_phases], ["scout_triage", "done"])
        self.assertIn("Scout is triaging", triage_phases[0]["status_text"])

    def test_inspection_script_passes_verbatim_up_to_12k_and_extracts_when_oversized(self) -> None:
        db_path = Path(self.tmp.name) / "script_budget.db"
        db = self.db_mod.CompanionDB(db_path)
        proj = db.create_project(self.models_mod.ProjectCreate(name="ScriptBudget", root_path=str(self.root)))
        sess = db.create_session(self.models_mod.SessionCreate(project_id=proj.id, title="Bridge", api_enabled=False))

        # 1. Passing script under 12k chars (~6,000 chars) even WITH focus -> 0 Flash Lite calls, raw stdout returned
        models_unused = _ScriptedModels([])
        gw_unused = _FakeGeminiGateway(SimpleNamespace(models=models_unused))
        gate_med = db.create_gate(
            sess.id,
            "propose_inspection_script",
            {
                "path": "inspect_med.py",
                "content": "print('ATTR_EXACT=42\\n' * 400)\n",
                "summary": "Check ATTR_EXACT value",
                "focus": "What is ATTR_EXACT?",
            },
            {"summary": "Check ATTR_EXACT value", "delete_on_success": True},
        )
        req_unused = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=gw_unused)))
        with patch.object(self.chat_rt, "_get_effective_scout_settings", return_value={"enabled": True, "summarize_threshold_chars": 4000}):
            res_med = asyncio.run(
                self.chat_rt.resolve_tool_gate(
                    gate_med["id"],
                    SimpleNamespace(action="write_run_and_delete", reason=None),
                    req_unused,
                )
            )
        self.assertEqual(len(models_unused.calls), 0, "Passing inspection script <=12k chars must NOT trigger Flash Lite")
        self.assertIn("ATTR_EXACT=42", res_med["bridge_reply_text"])
        self.assertNotIn("Scout Summary", res_med["bridge_reply_text"])

        # 2. Oversized script >12k chars -> triggers Flash Lite with concrete extraction prompt + goal + exec_id hint
        models_big = _ScriptedModels([
            _make_response(text="- `modal_user_profile.html:44`: `data-mandatory=\"1\"`\n- `user.py:91`: `country_region`")
        ])
        gw_big = _FakeGeminiGateway(SimpleNamespace(models=models_big))
        gate_big = db.create_gate(
            sess.id,
            "propose_inspection_script",
            {
                "path": "inspect_big.py",
                "content": "print('LINE_DATA=1234567890\\n' * 800)\n",
                "summary": "Extract mandatory modal attributes",
            },
            {"summary": "Extract mandatory modal attributes", "delete_on_success": True},
        )
        req_big = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=gw_big)))
        with patch.object(self.chat_rt, "_get_effective_scout_settings", return_value={"enabled": True, "summarize_threshold_chars": 4000}):
            res_big = asyncio.run(
                self.chat_rt.resolve_tool_gate(
                    gate_big["id"],
                    SimpleNamespace(action="write_run_and_delete", reason=None),
                    req_big,
                )
            )
        self.assertEqual(len(models_big.calls), 1)
        sent_prompt = models_big.calls[0]["contents"]
        self.assertIn("Extract mandatory modal attributes", sent_prompt)
        self.assertIn("Extract CONCRETE data", sent_prompt)
        self.assertIn("Scout Summary:", res_big["bridge_reply_text"])
        self.assertIn("read_command_output", res_big["bridge_reply_text"])

    def test_command_output_endpoint_and_deep_scout_defaults(self) -> None:
        output_buffer.clear_buffer()
        eid = output_buffer.record_command_output(
            "python developer/probe.py",
            "FULL_RAW_LINE_1\nFULL_RAW_LINE_2\n",
            exit_code=0,
            duration_ms=88,
        )
        found = self.workspace_rt.get_buffered_command_output(exec_id=eid)
        self.assertTrue(found["found"])
        self.assertEqual(found["exec_id"], eid)
        self.assertIn("FULL_RAW_LINE_1", found["output"])
        self.assertGreaterEqual(found["estimated_tokens"], 1)

        missing = self.workspace_rt.get_buffered_command_output(exec_id="missing99")
        self.assertFalse(missing["found"])
        self.assertIn("missing99", missing["error"])

        gemini_client = importlib.import_module("services.gemini_client")
        project = self._make_project()
        prev = gemini_client.prepare_tool_gate_preview(
            project, "scout_workspace", {"goal": "Trace modal wiring"}
        )
        self.assertEqual(prev["max_steps"], 10)
        self.assertIn("verbatim", self.local_scout._SCOUT_SYSTEM_PROMPT.lower())
        self.assertIn("wiring", self.local_scout._SCOUT_SYSTEM_PROMPT.lower())



@unittest.skipUnless(HAS_WEB_DEPS, "fastapi/httpx/google-genai required for HTTP TestClient integration tests")
class TestScoutApiAndBridgeWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        local_scout.clear_cooldowns()
        output_buffer.clear_buffer()
        self.tmp = tempfile.TemporaryDirectory()
        self.proj_dir = Path(self.tmp.name) / "repo"
        self.proj_dir.mkdir()
        (self.proj_dir / "mod.py").write_text(
            '"""Old header."""\n\ndef greet():\n    return "hi"\n',
            encoding="utf-8",
        )
        self.scripted_turns: List[Any] = []
        self.models = _ScriptedModels(self.scripted_turns)
        self.fake_client = SimpleNamespace(models=self.models)
        self.fake_client.models.list = lambda: [
            SimpleNamespace(name="models/gemini-3.1-flash-lite", display_name="Gemini 3.1 Flash Lite", supported_actions=["generateContent"], input_token_limit=1048576),
            SimpleNamespace(name="models/gemini-2.5-flash", display_name="Gemini 2.5 Flash", supported_actions=["generateContent"], input_token_limit=1048576),
        ]
        gateway = GeminiGateway(client_factory=lambda: self.fake_client)
        self.app = create_app(db_path=Path(self.tmp.name) / "companion.db", gemini_gateway=gateway)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        local_scout.clear_cooldowns()
        output_buffer.clear_buffer()
        self.tmp.cleanup()

    def test_api_scout_auto_approve_and_update_header_staging(self) -> None:
        self.client.post("/api/settings/scout", json={"enabled": True, "auto_approve": False})
        proj = self.client.post("/api/projects", json={"name": "Repo", "root_path": str(self.proj_dir)}).json()
        sess = self.client.post("/api/sessions", json={"project_id": proj["id"], "title": "API", "api_enabled": True}).json()

        self.scripted_turns.extend([
            _make_response(text="Scouting...", function_calls=[("scout_workspace", {"goal": "Find greet"})]),
            _make_response(function_calls=[("search_code", {"pattern": "greet"})]),
            _make_response(text="greet is in mod.py:3."),
            _make_response(text="Checking again...", function_calls=[("scout_workspace", {"goal": "Verify greet line"})]),
            _make_response(text="Confirmed greet is on mod.py:3."),
            _make_response(text="Updating header.", function_calls=[("update_header", {"paths": ["mod.py"]})]),
            _make_response(text='"""Greeting helper module exporting greet()."""'),
            _make_response(text="Staged updated header diff for review."),
        ])

        r1 = self.client.post("/api/chat/send", json={"session_id": sess["id"], "prompt": "Check greet & update header"}).json()
        self.assertEqual(len(r1["pending_gates"]), 1)
        g_scout = r1["pending_gates"][0]
        self.assertEqual(g_scout["tool_name"], "scout_workspace")

        r2 = self.client.post(f"/api/chat/gate/{g_scout['id']}", json={"action": "approve_and_auto"}).json()
        self.assertTrue(r2["scout_auto_approve_enabled"])
        self.assertEqual(len(r2["pending_gates"]), 1)
        g_hdr = r2["pending_gates"][0]
        self.assertEqual(g_hdr["tool_name"], "update_header")

        r3 = self.client.post(f"/api/chat/gate/{g_hdr['id']}", json={"action": "approve"}).json()
        self.assertIn("Old header.", (self.proj_dir / "mod.py").read_text(encoding="utf-8"))
        write_gates = [g for g in r3["pending_gates"] if g["tool_name"] == "write_file"]
        self.assertEqual(len(write_gates), 1)

        self.client.post(f"/api/chat/gate/{write_gates[0]['id']}", json={"action": "approve"})
        self.assertIn("Greeting helper module exporting greet().", (self.proj_dir / "mod.py").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("git") is not None, "git executable required")
    def test_git_draft_with_scout_and_update_headers_route(self) -> None:
        subprocess.run(["git", "init"], cwd=self.proj_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=self.proj_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Dev"], cwd=self.proj_dir, check=True)
        subprocess.run(["git", "add", "."], cwd=self.proj_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.proj_dir, check=True, stdout=subprocess.DEVNULL)
        (self.proj_dir / "mod.py").write_text('"""Old header."""\n\ndef greet(name="world"):\n    return f"hi {name}"\n', encoding="utf-8")

        proj = self.client.post("/api/projects", json={"name": "Repo", "root_path": str(self.proj_dir)}).json()
        sess = self.client.post("/api/sessions", json={"project_id": proj["id"], "title": "Bridge", "api_enabled": False}).json()

        self.client.post("/api/settings/scout", json={"enabled": True})
        self.scripted_turns.extend([
            _make_response(text="feat(mod): add optional name parameter to greet"),
            _make_response(text='"""Greeting helpers supporting parameterized greet(name)."""'),
        ])

        draft = self.client.post("/api/git/draft-message", json={"project_id": proj["id"], "use_scout": True}).json()
        self.assertTrue(draft["used_scout"])
        self.assertEqual(draft["message"], "feat(mod): add optional name parameter to greet")

        hdr_stage = self.client.post("/api/git/update-headers", json={"project_id": proj["id"], "session_id": sess["id"]}).json()
        self.assertEqual(hdr_stage["staged_count"], 1)
        self.assertEqual(hdr_stage["staged_paths"], ["mod.py"])
        self.assertEqual(len(hdr_stage["pending_gates"]), 1)
        self.assertEqual(hdr_stage["pending_gates"][0]["tool_name"], "write_file")


if __name__ == "__main__":
    unittest.main()
