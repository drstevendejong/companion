"""Zero-token unit test verifying thought_signature extraction and history replay."""

from __future__ import annotations

import base64
import importlib
import json
import sys
import types as pytypes
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))


def _make_stub_modules():
    class _Schema:
        def __init__(self, **_kwargs):
            pass

    class _Tool:
        def __init__(self, **_kwargs):
            pass

    class _Type:
        OBJECT = "OBJECT"
        STRING = "STRING"
        INTEGER = "INTEGER"
        BOOLEAN = "BOOLEAN"

    class _ThinkingLevel:
        LOW = "LOW"
        MEDIUM = "MEDIUM"
        HIGH = "HIGH"
        XHIGH = "XHIGH"

    class _FunctionCall:
        def __init__(self, name: str, args: dict | None = None, id: str | None = None):
            self.name = name
            self.args = args or {}
            self.id = id

    class _FunctionResponse:
        def __init__(self, name: str, response: dict | None = None, id: str | None = None):
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
        ):
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
        def __init__(self, role: str, parts: list):
            self.role = role
            self.parts = parts

    class _ThinkingConfig:
        def __init__(self, **_kwargs):
            pass

    class _FunctionDeclaration:
        def __init__(self, **_kwargs):
            pass

    class _GenerateContentConfig:
        def __init__(self, **_kwargs):
            pass

    genai_types = pytypes.ModuleType("google.genai.types")
    genai_types.Schema = _Schema
    genai_types.Tool = _Tool
    genai_types.Type = _Type
    genai_types.ThinkingLevel = _ThinkingLevel
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

    pydantic_mod = pytypes.ModuleType("pydantic")

    class _BaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    def _Field(default=None, default_factory=None, **_kwargs):
        return default_factory() if default_factory is not None else default

    pydantic_mod.BaseModel = _BaseModel
    pydantic_mod.Field = _Field

    fastapi_mod = pytypes.ModuleType("fastapi")

    class _APIRouter:
        def __init__(self, **_kwargs):
            pass

        def get(self, *_a, **_kw):
            return lambda fn: fn

        def post(self, *_a, **_kw):
            return lambda fn: fn

        def patch(self, *_a, **_kw):
            return lambda fn: fn

        def delete(self, *_a, **_kw):
            return lambda fn: fn

    fastapi_mod.APIRouter = _APIRouter
    fastapi_mod.HTTPException = Exception
    fastapi_mod.Query = lambda default=None, **_kw: default
    fastapi_mod.Request = object

    return {
        "google": google_mod,
        "google.genai": genai_mod,
        "google.genai.types": genai_types,
        "pydantic": pydantic_mod,
        "fastapi": fastapi_mod,
    }


class TestThoughtSignatureRoundtrip(unittest.TestCase):
    def test_extracts_and_replays_thought_signature_on_function_call(self) -> None:
        stubs = _make_stub_modules()
        target_mods = ("models", "services.gemini_client", "routes.chat")
        saved_mods = {m: sys.modules.get(m) for m in target_mods}
        try:
            with patch.dict(sys.modules, stubs, clear=False):
                for mod_name in target_mods:
                    sys.modules.pop(mod_name, None)

                gemini_client = importlib.import_module("services.gemini_client")
                chat_routes = importlib.import_module("routes.chat")

                sig_bytes = b"\x01\x02thought-sig-bytes\xff"
                class _NoTextPropResponse:
                    candidates = [
                        SimpleNamespace(
                            content=SimpleNamespace(
                                parts=[
                                    SimpleNamespace(text="internal reasoning", thought=True, function_call=None),
                                    SimpleNamespace(text="Checking tree.", thought=False, function_call=None),
                                    SimpleNamespace(
                                        text=None,
                                        thought=False,
                                        thought_signature=sig_bytes,
                                        function_call=SimpleNamespace(
                                            id="call-42",
                                            name="list_tree",
                                            args={"subpath": ".", "max_depth": 3},
                                        ),
                                    ),
                                ]
                            )
                        )
                    ]
                    usage_metadata = None

                    @property
                    def text(self):
                        raise AssertionError("response.text should not be accessed when parts/function_calls exist")

                fake_response = _NoTextPropResponse()

                fake_client = SimpleNamespace(
                    models=SimpleNamespace(generate_content=lambda **_kw: fake_response)
                )
                gateway = gemini_client.GeminiGateway(client_factory=lambda: fake_client)
                session = SimpleNamespace(model="gemini-4-hydrogen", thinking_level="HIGH")
                project = SimpleNamespace(root_path=str(COMPANION_ROOT), directives_path=None)

                turn = gateway.generate_turn(session, project, [])
                self.assertEqual(turn["text"], "Checking tree.")
                self.assertEqual(len(turn["function_calls"]), 1)
                tc = turn["function_calls"][0]
                self.assertEqual(tc["id"], "call-42")
                self.assertEqual(base64.b64decode(tc["thought_signature_b64"]), sig_bytes)

                # Verify replay reconstructs FunctionCall + thought_signature + merges tool_event + follow-up user prompt
                msgs = [
                    SimpleNamespace(role="user", text="List tree", attachments=[]),
                    SimpleNamespace(role="model", text=turn["text"], tool_calls=turn["function_calls"]),
                    SimpleNamespace(
                        role="tool_event",
                        text=json.dumps({
                            "tool_name": "list_tree",
                            "call_id": "call-42",
                            "response": {"subpath": ".", "tree": "main.py"},
                        }),
                    ),
                    SimpleNamespace(role="user", text="Tell me what happened so far & retry", attachments=[]),
                ]
                history = chat_routes._messages_to_history(msgs)
                self.assertEqual([c.role for c in history], ["user", "model", "user"])
                model_msg = history[1]
                fc_parts = [p for p in model_msg.parts if p.function_call is not None]
                self.assertEqual(len(fc_parts), 1)
                self.assertEqual(fc_parts[0].thought_signature, sig_bytes)
                self.assertEqual(fc_parts[0].function_call.id, "call-42")

                merged_user_msg = history[2]
                self.assertEqual(len(merged_user_msg.parts), 2)
                self.assertEqual(merged_user_msg.parts[0].function_response.id, "call-42")
                self.assertEqual(merged_user_msg.parts[1].text, "Tell me what happened so far & retry")
        finally:
            for mod_name, orig in saved_mods.items():
                if orig is None:
                    sys.modules.pop(mod_name, None)
                else:
                    sys.modules[mod_name] = orig


if __name__ == "__main__":
    unittest.main()
