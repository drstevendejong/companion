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


if __name__ == "__main__":
    unittest.main()
