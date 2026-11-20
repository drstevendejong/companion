"""Zero-token end-to-end workflow test with mocked Gemini boundary and FastAPI TestClient."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))

HAS_DEPS = all(
    importlib.util.find_spec(mod) is not None
    for mod in ("fastapi", "httpx", "pydantic", "google.genai")
)

if HAS_DEPS:
    from fastapi.testclient import TestClient
    from app import create_app
    from services.gemini_client import GeminiGateway


class _FakeModels:
    def __init__(self, scripted_turns: List[Any]) -> None:
        self._turns = list(scripted_turns)
        self.seen_contents: List[Any] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.seen_contents.append(kwargs.get("contents"))
        if not self._turns:
            raise AssertionError("Unexpected extra Gemini API call in test.")
        return self._turns.pop(0)


class _FakeClient:
    def __init__(self, scripted_turns: List[Any]) -> None:
        self.models = _FakeModels(scripted_turns)


def _make_response(
    text: str = "",
    function_calls: List[tuple[str, dict]] | None = None,
    thought_signature: bytes | None = None,
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
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
        usage_metadata=SimpleNamespace(
            prompt_token_count=120,
            candidates_token_count=30,
            thoughts_token_count=15,
            total_token_count=165,
        ),
    )


@unittest.skipUnless(HAS_DEPS, "fastapi/httpx/google-genai not installed yet (auto-installed at startup)")
class TestHumanGatedWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.proj_dir = Path(self.tmp.name) / "target_project"
        self.proj_dir.mkdir()
        self.target_file = self.proj_dir / "service.py"
        self.target_file.write_text("STATUS = 'broken'\n", encoding="utf-8")

        self.fake_client = _FakeClient([
            # Turn 1: Gemini asks to read lines 1-5 of service.py
            _make_response(
                text="Let me check service.py first.",
                function_calls=[("read_lines", {"path": "service.py", "start_line": 1, "end_line": 5})],
                thought_signature=b"sig-turn-1",
            ),
            # Turn 2: After user approves read, Gemini proposes a surgical patch
            _make_response(
                text="Found it. Proposing fix.",
                function_calls=[(
                    "patch_literal",
                    {
                        "path": "service.py",
                        "old_text": "STATUS = 'broken'",
                        "new_text": "STATUS = 'fixed'",
                        "summary": "Fix status constant",
                    },
                )],
                thought_signature=b"sig-turn-2",
            ),
            # Turn 3: After user approves patch, Gemini confirms
            _make_response(text="Patch applied. Run your tests to verify."),
        ])

        gateway = GeminiGateway(client_factory=lambda: self.fake_client)
        db_path = Path(self.tmp.name) / "companion.db"
        self.client = TestClient(create_app(db_path=db_path, gemini_gateway=gateway))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_gated_read_then_patch_then_project_backup_undo(self) -> None:
        proj = self.client.post("/api/projects", json={
            "name": "Target",
            "root_path": str(self.proj_dir),
            "backup_dir": "backup",
        }).json()
        sess = self.client.post("/api/sessions", json={
            "project_id": proj["id"],
            "title": "Fix Status",
            "api_enabled": True,
        }).json()

        # Step 1: Send user prompt -> pauses at read_lines gate
        res1 = self.client.post("/api/chat/send", json={
            "session_id": sess["id"],
            "prompt": "Fix STATUS in service.py",
        }).json()
        self.assertEqual(len(res1["pending_gates"]), 1)
        read_gate = res1["pending_gates"][0]
        self.assertEqual(read_gate["tool_name"], "read_lines")
        self.assertGreater(read_gate["preview"]["estimated_tokens"], 0)

        # Step 2: Approve read -> Gemini proposes patch_literal gate
        res2 = self.client.post(f"/api/chat/gate/{read_gate['id']}", json={"action": "approve"}).json()
        model_contents = self.fake_client.models.seen_contents[1]
        replayed_fc_parts = [
            p for c in model_contents if c.role == "model" for p in c.parts if p.function_call is not None
        ]
        self.assertEqual(replayed_fc_parts[0].thought_signature, b"sig-turn-1")
        self.assertEqual(len(res2["pending_gates"]), 1)
        patch_gate = res2["pending_gates"][0]
        self.assertEqual(patch_gate["tool_name"], "patch_literal")
        self.assertIn("+STATUS = 'fixed'", patch_gate["preview"]["diff"])

        # File must STILL be untouched before approval
        self.assertIn("broken", self.target_file.read_text(encoding="utf-8"))

        # Step 3: Approve patch -> file modified + backup stored in project's backup/
        res3 = self.client.post(f"/api/chat/gate/{patch_gate['id']}", json={"action": "approve"}).json()
        self.assertEqual(res3["pending_gates"], [])
        self.assertIn("fixed", self.target_file.read_text(encoding="utf-8"))
        patch_id = res3["patch_record"]["id"]

        # Step 4: Undo patch restores original file from project backup/
        undo = self.client.post(f"/api/workspace/undo/{patch_id}").json()
        self.assertEqual(undo["action"], "restored_from_backup")
        self.assertIn("broken", self.target_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
