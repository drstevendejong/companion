"""Zero-token unit and route tests for local API key persistence and settings modal endpoints."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))

from services import key_store

HAS_WEB_DEPS = all(
    importlib.util.find_spec(mod) is not None
    for mod in ("fastapi", "httpx", "pydantic", "google.genai")
)

if HAS_WEB_DEPS:
    from fastapi.testclient import TestClient
    from app import create_app
    from services.gemini_client import GeminiGateway


class TestKeyStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.key_file = Path(self.tmp.name) / "data" / "gemini_key.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_save_load_mask_and_clear_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            raw, src = key_store.load_api_key(self.key_file)
            self.assertEqual(raw, "")
            self.assertEqual(src, "none")

            key_store.save_api_key("AIzaTestKey1234567890", self.key_file)
            self.assertTrue(self.key_file.is_file())

            loaded, src2 = key_store.load_api_key(self.key_file)
            self.assertEqual(loaded, "AIzaTestKey1234567890")
            self.assertEqual(src2, "file")
            self.assertEqual(key_store.mask_api_key(loaded), "AIza••••7890")

            self.assertTrue(key_store.clear_api_key(self.key_file))
            self.assertFalse(self.key_file.exists())


@unittest.skipUnless(HAS_WEB_DEPS, "fastapi/httpx/google-genai not installed yet (auto-installed at startup)")
class TestSettingsApiKeyRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.key_file = Path(self.tmp.name) / "gemini_key.json"
        self.db_path = Path(self.tmp.name) / "companion.db"

        def fake_validator(k: str) -> tuple[bool, str]:
            if k.startswith("AIzaValid"):
                return True, ""
            return False, "API_KEY_INVALID"

        gateway = GeminiGateway(key_path=self.key_file, key_validator=fake_validator)
        self.client = TestClient(create_app(db_path=self.db_path, gemini_gateway=gateway))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_startup_missing_then_invalid_then_valid_save(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            r0 = self.client.get("/api/settings/api-key?verify=true").json()
            self.assertFalse(r0["configured"])
            self.assertFalse(r0["valid"])

            bad = self.client.post("/api/settings/api-key", json={"api_key": "bad-key", "validate_key": True})
            self.assertEqual(bad.status_code, 400)
            self.assertFalse(self.key_file.exists())

            ok = self.client.post("/api/settings/api-key", json={"api_key": "AIzaValidKey9999", "validate_key": True})
            self.assertEqual(ok.status_code, 200)
            self.assertTrue(ok.json()["saved"])
            self.assertEqual(ok.json()["masked_preview"], "AIza••••9999")

            r1 = self.client.get("/api/settings/api-key?verify=true").json()
            self.assertTrue(r1["configured"])
            self.assertTrue(r1["valid"])
            self.assertEqual(r1["source"], "file")


class TestCompanionPromptSettings(unittest.TestCase):
    def test_companion_prompt_settings_save_disable_and_reset(self) -> None:
        import importlib
        import types as pytypes
        from types import SimpleNamespace

        has_pydantic = importlib.util.find_spec("pydantic") is not None
        saved_mods = {
            k: sys.modules.get(k)
            for k in (
                "pydantic",
                "fastapi",
                "google",
                "google.genai",
                "google.genai.types",
                "models",
                "storage.db",
                "services.gemini_client",
                "services.bridge",
                "routes.settings",
            )
        }
        try:
            extra_mods: dict[str, object] = {}
            if not has_pydantic:
                pydantic_mod = pytypes.ModuleType("pydantic")

                class _BaseModel:
                    def __init__(self, **kw):
                        for k, v in kw.items():
                            setattr(self, k, v)

                    def model_dump(self, **_kw):
                        return dict(self.__dict__)

                pydantic_mod.BaseModel = _BaseModel
                pydantic_mod.Field = lambda default=None, default_factory=None, **_kw: (
                    default_factory() if default_factory else default
                )
                extra_mods["pydantic"] = pydantic_mod

            if importlib.util.find_spec("google") is None or importlib.util.find_spec("google.genai") is None:
                genai_types = pytypes.ModuleType("google.genai.types")
                genai_types.GenerateContentConfig = object
                genai_types.Schema = lambda **_kw: None
                genai_types.Tool = lambda **_kw: None
                genai_types.Type = SimpleNamespace(
                    OBJECT="OBJECT", STRING="STRING", INTEGER="INTEGER", BOOLEAN="BOOLEAN"
                )
                genai_types.ThinkingLevel = SimpleNamespace(LOW="LOW", MEDIUM="MEDIUM", HIGH="HIGH", XHIGH="XHIGH")
                genai_types.ThinkingConfig = lambda **_kw: None
                genai_types.FunctionDeclaration = lambda **_kw: None
                genai_mod = pytypes.ModuleType("google.genai")
                genai_mod.types = genai_types
                genai_mod.Client = object
                google_mod = pytypes.ModuleType("google")
                google_mod.genai = genai_mod
                extra_mods["google"] = google_mod
                extra_mods["google.genai"] = genai_mod
                extra_mods["google.genai.types"] = genai_types

            fastapi_mod = pytypes.ModuleType("fastapi")
            fastapi_mod.APIRouter = lambda **_kw: SimpleNamespace(
                post=lambda *_a, **_k: (lambda fn: fn),
                get=lambda *_a, **_k: (lambda fn: fn),
                delete=lambda *_a, **_k: (lambda fn: fn),
            )
            fastapi_mod.HTTPException = Exception
            fastapi_mod.Query = lambda default=None, **_kw: default
            fastapi_mod.Request = object
            extra_mods["fastapi"] = fastapi_mod

            for k in (
                "models",
                "storage.db",
                "services.gemini_client",
                "services.bridge",
                "routes.settings",
            ):
                sys.modules.pop(k, None)

            with patch.dict(sys.modules, extra_mods, clear=False), tempfile.TemporaryDirectory() as tmp:
                from models import CompanionSettingsUpdate
                from routes import settings as route_settings
                from services import bridge as bridge_svc
                from services import gemini_client
                from storage.db import CompanionDB

                db = CompanionDB(Path(tmp) / "settings.db")
                req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=None)))

                s0 = route_settings.get_companion_settings_endpoint(req)
                self.assertEqual(s0["bridge_guardrail"], bridge_svc.DEFAULT_BRIDGE_GUARDRAIL)
                self.assertEqual(s0["system_prompt"], gemini_client.get_default_system_prompt())
                self.assertEqual(s0["bridge_tool_contract"], bridge_svc.get_default_playground_tool_contract())
                self.assertFalse(s0["customized"]["bridge_guardrail"])

                # Save custom overrides and explicitly disable guardrail with empty string
                s1 = route_settings.update_companion_settings_endpoint(
                    CompanionSettingsUpdate(
                        bridge_guardrail="",
                        system_prompt="Custom API System Prompt",
                        bridge_tool_contract="Custom Bridge Contract",
                    ),
                    req,
                )
                self.assertEqual(s1["bridge_guardrail"], "")
                self.assertTrue(s1["customized"]["bridge_guardrail"])
                self.assertEqual(bridge_svc.get_bridge_guardrail(db), "")
                self.assertEqual(gemini_client.get_base_system_prompt(db), "Custom API System Prompt")
                self.assertEqual(bridge_svc.get_playground_tool_contract(db), "Custom Bridge Contract")

                # Reset system_prompt only, leaving others intact
                s2 = route_settings.update_companion_settings_endpoint(
                    CompanionSettingsUpdate(reset_keys=["system_prompt"]),
                    req,
                )
                self.assertEqual(s2["system_prompt"], gemini_client.get_default_system_prompt())
                self.assertFalse(s2["customized"]["system_prompt"])
                self.assertEqual(s2["bridge_guardrail"], "")
                self.assertEqual(s2["bridge_tool_contract"], "Custom Bridge Contract")

                # Reset all back to defaults
                s3 = route_settings.update_companion_settings_endpoint(
                    CompanionSettingsUpdate(
                        reset_keys=["bridge_guardrail", "system_prompt", "bridge_tool_contract"]
                    ),
                    req,
                )
                self.assertEqual(s3["bridge_guardrail"], bridge_svc.DEFAULT_BRIDGE_GUARDRAIL)
                self.assertFalse(s3["customized"]["bridge_guardrail"])
                self.assertFalse(s3["customized"]["bridge_tool_contract"])
        finally:
            for k, orig in saved_mods.items():
                if orig is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = orig


if __name__ == "__main__":
    unittest.main()
