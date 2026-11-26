"""Zero-token unit tests for Playground Bridge (.md/JSON/Python parser, 1-time Directives bundler,
generated .md output discovery) and SQLite RPM/TPM/RPD rate telemetry + Pacific 00:00 reset."""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
import sys
import tempfile
import types as pytypes
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

COMPANION_ROOT = Path(__file__).resolve().parent.parent
if str(COMPANION_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPANION_ROOT))


def _load_bridge_with_stubs():
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

    genai_types = pytypes.ModuleType("google.genai.types")
    genai_types.GenerateContentConfig = object
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

    return patch.dict(
        sys.modules,
        {
            "pydantic": pydantic_mod,
            "google": google_mod,
            "google.genai": genai_mod,
            "google.genai.types": genai_types,
        },
        clear=False,
    )


class TestPlaygroundBridge(unittest.TestCase):
    def test_parses_copy_as_markdown_with_prose_and_nested_blocks(self) -> None:
        with _load_bridge_with_stubs():
            for mod_name in ("models", "services.gemini_client", "services.bridge"):
                sys.modules.pop(mod_name, None)
            bridge = importlib.import_module("services.bridge")

            md = (
                "Step 1/2: Applying fix and inspecting library.\n\n"
                "````markdown\n"
                "### Patch\n"
                "```json\n"
                "[\n"
                '  {"tool": "patch_literal", "path": "app.py", "old_text": "x=1", "new_text": "x=2", "summary": "Bump x"},\n'
                '  {"tool": "read_lines", "path": "lib.py", "start_line": 10, "end_line": 30}\n'
                "]\n"
                "```\n\n"
                "Wait for output.\n"
                "## Local Workspace Tool Results\n"
                "- **read_lines**: hallucinated response\n"
                "```json\n"
                '[{"tool": "delete_file", "path": "secret.py"}]\n'
                "```\n"
                "````\n"
            )
            parsed = bridge.parse_playground_markdown(md)
            tools = [a["tool"] for a in parsed["actions"]]
            self.assertEqual(tools, ["patch_literal", "read_lines"])
            self.assertEqual(parsed["actions"][0]["args"]["path"], "app.py")
            # Hard stop truncates runaway continuation & fake tool results after first ```json block
            self.assertNotIn("Wait for output", parsed["prose_text"])
            self.assertNotIn("Local Workspace Tool Results", parsed["prose_text"])
            self.assertNotIn("secret.py", parsed["prose_text"])
            self.assertIn("Step 1/2: Applying fix", parsed["prose_text"])

            # Verify inline JSON also truncates trailing hallucinated continuation
            inline_runaway = (
                'Checking tree now.\n'
                '{"tool": "list_tree", "subpath": "."}\n'
                'Wait for output...\n'
                '## Local Workspace Tool Results\n'
                'fake tree'
            )
            parsed_inline = bridge.parse_playground_markdown(inline_runaway)
            self.assertEqual([a["tool"] for a in parsed_inline["actions"]], ["list_tree"])
            self.assertNotIn("Local Workspace Tool Results", parsed_inline["prose_text"])

    def test_parses_standalone_python_packer_without_json(self) -> None:
        with _load_bridge_with_stubs():
            for mod_name in ("models", "services.gemini_client", "services.bridge"):
                sys.modules.pop(mod_name, None)
            bridge = importlib.import_module("services.bridge")

            md = (
                "Run `/packer.py` to generate `packed_context.md`:\n\n"
                "```python\n"
                "from pathlib import Path\n"
                "Path('packed_context.md').write_text('# hi', encoding='utf-8')\n"
                "```\n"
            )
            parsed = bridge.parse_playground_markdown(md)
            self.assertEqual(len(parsed["actions"]), 1)
            self.assertEqual(parsed["actions"][0]["tool"], "propose_script")
            self.assertEqual(parsed["actions"][0]["args"]["path"], "packer.py")

    def test_parses_concatenated_json_and_unfenced_inline_list_tree(self) -> None:
        with _load_bridge_with_stubs():
            for mod_name in ("models", "services.gemini_client", "services.bridge"):
                sys.modules.pop(mod_name, None)
            bridge = importlib.import_module("services.bridge")

            concat_md = (
                "```json\n"
                '{"tool": "list_tree", "subpath": "."}\n'
                '{"tool": "read_file", "path": "app.py"}\n'
                "```\n"
            )
            p1 = bridge.parse_playground_markdown(concat_md)
            self.assertEqual([a["tool"] for a in p1["actions"]], ["list_tree", "read_file"])

            inline_md = 'Checking the workspace layout:\n{"tool": "list_tree", "path": ".", "max_depth": 3}'
            p2 = bridge.parse_playground_markdown(inline_md)
            self.assertEqual(len(p2["actions"]), 1)
            self.assertEqual(p2["actions"][0]["tool"], "list_tree")
            self.assertEqual(p2["actions"][0]["args"]["max_depth"], 3)

    def test_build_prompt_bundle_respects_once_per_session_directives(self) -> None:
        with _load_bridge_with_stubs(), tempfile.TemporaryDirectory() as tmp:
            for mod_name in ("models", "services.gemini_client", "services.bridge"):
                sys.modules.pop(mod_name, None)
            bridge = importlib.import_module("services.bridge")

            (Path(tmp) / "Directives.md").write_text("Rule 1: Keep it clean.", encoding="utf-8")
            proj = SimpleNamespace(
                name="Demo",
                root_path=tmp,
                directives_path="Directives.md",
                ignore_patterns=[],
            )
            atts = [SimpleNamespace(kind="image", label="shot.png", content="data:image/png;base64,abc")]

            b1 = bridge.build_prompt_bundle(proj, "Check layout", atts, include_directives=True)
            self.assertTrue(b1["directives_included"])
            self.assertIn("COMPANION BRIDGE TOOL CONTRACT", b1["bundle_text"])
            self.assertIn("plain-English one-liner", b1["bundle_text"])
            self.assertIn("AT MOST ONE", b1["bundle_text"])
            self.assertIn("ONCE ONLY", b1["bundle_text"])
            self.assertIn("Rule 1: Keep it clean.", b1["bundle_text"])
            self.assertIn("Copy Image", b1["bundle_text"])
            self.assertNotIn("Copy CDP Image", b1["bundle_text"])
            self.assertEqual(b1["image_count"], 1)

            b2 = bridge.build_prompt_bundle(proj, "Follow-up prompt", [], include_directives=False)
            self.assertFalse(b2["directives_included"])
            self.assertNotIn("COMPANION BRIDGE TOOL CONTRACT", b2["bundle_text"])
            self.assertEqual(b2["bundle_text"], "Follow-up prompt")

    def test_find_recent_markdown_outputs_picks_generated_md(self) -> None:
        with _load_bridge_with_stubs(), tempfile.TemporaryDirectory() as tmp:
            for mod_name in ("models", "services.gemini_client", "services.bridge"):
                sys.modules.pop(mod_name, None)
            bridge = importlib.import_module("services.bridge")

            (Path(tmp) / "packed_context.md").write_text("# Packed\nsome code", encoding="utf-8")
            res = bridge.find_recent_markdown_outputs(tmp, stdout_text="Wrote packed_context.md (20 bytes)")
            self.assertTrue(any(r["path"] == "packed_context.md" for r in res))

    def test_format_trailing_tool_events_and_snippet_unwrapping(self) -> None:
        with _load_bridge_with_stubs(), tempfile.TemporaryDirectory() as tmp:
            for mod_name in ("models", "services.gemini_client", "services.bridge"):
                sys.modules.pop(mod_name, None)
            bridge = importlib.import_module("services.bridge")

            import json as _json

            messages = [
                SimpleNamespace(role="user", text="Find foo"),
                SimpleNamespace(role="model", text="Checking"),
                SimpleNamespace(
                    role="tool_event",
                    text=_json.dumps(
                        {
                            "tool_name": "read_file",
                            "response": {"path": "app.py", "total_lines": 2, "content": "1\n2"},
                        }
                    ),
                ),
            ]
            reply = bridge.format_trailing_tool_events(messages)
            self.assertIn("## Local Workspace Tool Results", reply)
            self.assertIn("`read_file` (`app.py`", reply)

            proj = SimpleNamespace(name="Demo", root_path=tmp, directives_path=None, ignore_patterns=[])
            att = SimpleNamespace(kind="snippet", label="Approved tool results (1)", content=reply)
            bundled = bridge.build_prompt_bundle(proj, "Action patch_literal(app.py) was denied. Reason: keep it", [att], include_directives=False)
            # Bridge tool-result snippet must be embedded as clean Markdown rather than fenced in ```text
            self.assertNotIn("```text\n## Local Workspace Tool Results", bundled["bundle_text"])
            self.assertIn("## Local Workspace Tool Results", bundled["bundle_text"])
            self.assertIn("Action patch_literal(app.py) was denied.", bundled["bundle_text"])

    def test_extract_failure_excerpt_and_failed_script_formatting(self) -> None:
        with _load_bridge_with_stubs():
            for mod_name in ("models", "services.gemini_client", "services.bridge"):
                sys.modules.pop(mod_name, None)
            bridge = importlib.import_module("services.bridge")

            noisy_ok = "\n".join(f"test_case_{i} ... ok" for i in range(180))
            after_ok = "\n".join(f"post_case_{i} ... ok" for i in range(180))
            big_log = (
                f"{noisy_ok}\n"
                "======================================================================\n"
                "FAIL: test_inbox_link (tests.test_inbox.InboxTest)\n"
                "Traceback (most recent call last):\n"
                '  File "tests/test_inbox.py", line 42, in test_inbox_link\n'
                "    self.assertEqual(200, 500)\n"
                "AssertionError: 200 != 500\n"
                "----------------------------------------------------------------------\n"
                f"{after_ok}\n"
                "FAILED (failures=1)\n"
            )
            excerpt = bridge.extract_failure_excerpt(big_log, max_chars=1200)
            self.assertIn("FAIL: test_inbox_link", excerpt)
            self.assertIn("AssertionError: 200 != 500", excerpt)
            self.assertIn("FAILED (failures=1)", excerpt)
            self.assertNotIn("test_case_0 ... ok", excerpt)
            self.assertNotIn("post_case_50 ... ok", excerpt)

            md = bridge.format_bridge_tool_results(
                [
                    {
                        "tool_name": "propose_script",
                        "response": {
                            "written": True,
                            "executed": True,
                            "path": "run_checks.py",
                            "exit_code": 1,
                            "has_traceback": False,
                            "output_preview": "ERROR: icon audit failed in core/ui",
                        },
                    }
                ]
            )
            self.assertIn("exit code `1`", md)
            self.assertIn("ERROR: icon audit failed in core/ui", md)

    def test_auto_approve_reads_never_auto_approves_writes_or_scripts(self) -> None:
        with _load_bridge_with_stubs(), tempfile.TemporaryDirectory() as tmp:
            fastapi_mod = pytypes.ModuleType("fastapi")
            fastapi_mod.APIRouter = lambda **_kw: SimpleNamespace(
                post=lambda *_a, **_k: (lambda fn: fn),
                get=lambda *_a, **_k: (lambda fn: fn),
            )
            fastapi_mod.HTTPException = Exception
            fastapi_mod.Query = lambda default=None: default
            fastapi_mod.Request = object

            for mod_name in ("models", "services.gemini_client", "services.bridge", "routes.bridge"):
                sys.modules.pop(mod_name, None)

            with patch.dict(sys.modules, {"fastapi": fastapi_mod}, clear=False):
                route_bridge = importlib.import_module("routes.bridge")
                models = importlib.import_module("models")

                (Path(tmp) / "app.py").write_text("x = 1\n", encoding="utf-8")

                class FakeDB:
                    def __init__(self) -> None:
                        self.messages = []
                        self.gates = []
                        self.session = SimpleNamespace(
                            id="s1", project_id="p1", api_enabled=False, directives_sent=True
                        )
                        self.project = SimpleNamespace(
                            id="p1",
                            name="Demo",
                            root_path=tmp,
                            python_path=None,
                            directives_path=None,
                            backup_dir="backup",
                            post_patch_command=None,
                            ignore_patterns=[],
                            preset_commands=[],
                        )

                    def get_session(self, sid):
                        return self.session if sid == "s1" else None

                    def get_project(self, pid):
                        return self.project if pid == "p1" else None

                    def add_message(self, sid, role, text, **kw):
                        self.messages.append(SimpleNamespace(id=f"m{len(self.messages)+1}", role=role, text=text, **kw))

                    def create_gate(self, **kw):
                        self.gates.append({"id": f"g{len(self.gates)+1}", "status": "pending", **kw})

                    def list_pending_gates(self, sid):
                        return [g for g in self.gates if g["status"] == "pending"]

                    def list_messages(self, sid):
                        return list(self.messages)

                db = FakeDB()
                req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))

                mixed_md = (
                    "```json\n"
                    "[\n"
                    '  {"tool": "read_file", "path": "app.py"},\n'
                    '  {"tool": "patch_literal", "path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},\n'
                    '  {"tool": "write_file", "path": "new.py", "content": "y = 3\\n"},\n'
                    '  {"tool": "propose_script", "path": "packer.py", "content": "print(1)"}\n'
                    "]\n"
                    "```"
                )
                payload = models.BridgeParseRequest(
                    session_id="s1", raw_markdown=mixed_md, auto_approve_reads=True
                )
                import asyncio as _asyncio
                res = _asyncio.run(route_bridge.parse_bridge_response(payload, req))
                self.assertEqual(res["auto_executed_count"], 1)
                self.assertEqual(res["staged_count"], 3)
                self.assertEqual(
                    [g["tool_name"] for g in res["pending_gates"]],
                    ["patch_literal", "write_file", "propose_script"],
                )
                # new.py must NOT be written, app.py must remain untouched
                self.assertFalse((Path(tmp) / "new.py").exists())
                self.assertEqual((Path(tmp) / "app.py").read_text(encoding="utf-8"), "x = 1\n")
                # Because pending_gates is non-empty, bridge_reply_text stays empty until gates resolve
                self.assertEqual(res["bridge_reply_text"], "")

                # Pure read-only payload returns bridge_reply_text immediately
                db2 = FakeDB()
                req2 = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db2)))
                reads_only = '```json\n{"tool": "read_file", "path": "app.py"}\n```'
                res2 = _asyncio.run(route_bridge.parse_bridge_response(
                    models.BridgeParseRequest(session_id="s1", raw_markdown=reads_only, auto_approve_reads=True),
                    req2,
                ))
                self.assertEqual(res2["auto_executed_count"], 1)
                self.assertEqual(res2["staged_count"], 0)
                self.assertIn("x = 1", res2["bridge_reply_text"])

                # Truncated tool JSON emits a clear diagnostic warning instead of failing silently
                db3 = FakeDB()
                req3 = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db3)))
                truncated = 'json\n[\n{\n"tool": "patch_literal",\n"path": "app.py",\n"old_text": "partial'
                res3 = _asyncio.run(route_bridge.parse_bridge_response(
                    models.BridgeParseRequest(session_id="s1", raw_markdown=truncated, auto_approve_reads=True),
                    req3,
                ))
                self.assertEqual(res3["parsed_count"], 0)
                self.assertTrue(res3["warnings"])
                self.assertIn("truncated", res3["warnings"][0].lower())

                # Mixed batch: 2 failed patch_literal + 1 valid patch_literal
                db4 = FakeDB()
                req4 = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db4)))
                mixed_err_md = (
                    "json\n[\n"
                    '  {"tool": "patch_literal", "path": "app.py", "old_text": "missing_1", "new_text": "a"},\n'
                    '  {"tool": "patch_literal", "path": "app.py", "old_text": "missing_2", "new_text": "b"},\n'
                    '  {"tool": "patch_literal", "path": "app.py", "old_text": "x = 1", "new_text": "x = 9"}\n'
                    "]"
                )
                res4 = _asyncio.run(route_bridge.parse_bridge_response(
                    models.BridgeParseRequest(session_id="s1", raw_markdown=mixed_err_md, auto_approve_reads=True),
                    req4,
                ))
                self.assertEqual(res4["error_count"], 2)
                self.assertEqual(res4["staged_count"], 1)
                self.assertEqual(len(res4["pending_gates"]), 1)
                self.assertEqual(res4["bridge_reply_text"], "")
                self.assertIn("## Local Workspace Tool Results", res4["error_reply_text"])
                self.assertIn("Target literal block not found in 'app.py'", res4["error_reply_text"])

                # All failed patch_literal: populates both bridge_reply_text and error_reply_text for auto-copy
                db5 = FakeDB()
                req5 = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db5)))
                all_err_md = (
                    '[{"tool": "patch_literal", "path": "app.py", "old_text": "missing", "new_text": "a"}]'
                )
                res5 = _asyncio.run(route_bridge.parse_bridge_response(
                    models.BridgeParseRequest(session_id="s1", raw_markdown=all_err_md, auto_approve_reads=True),
                    req5,
                ))
                self.assertEqual(res5["error_count"], 1)
                self.assertEqual(res5["staged_count"], 0)
                self.assertIn("Target literal block not found in 'app.py'", res5["bridge_reply_text"])
                self.assertEqual(res5["bridge_reply_text"], res5["error_reply_text"])

    def test_parses_sloppy_playground_json_with_bare_header_and_unescaped_html_quotes(self) -> None:
        with _load_bridge_with_stubs():
            for mod_name in ("models", "services.gemini_client", "services.bridge"):
                sys.modules.pop(mod_name, None)
            bridge = importlib.import_module("services.bridge")

            sloppy_with_header = (
                "json\n"
                "[\n"
                "{\n"
                '"tool": "patch_literal",\n'
                '"path": "core/ui/web/views/workspace_inbox.html",\n'
                '"old_text": " <div class="inbox-header-actions">\\n {RUN_SCAN_BTN_HTML}\\n '
                '<button type="button" class="primary-btn" onclick="openTrustManagerModal()">'
                'Trust Manager...</button>\\n </div>",\n'
                '"new_text": " <div class="inbox-header-actions">\\n {CHECK_EMAIL_BTN_HTML}\\n '
                '{RUN_SCAN_BTN_HTML}\\n <button type="button" class="primary-btn" '
                'onclick="openTrustManagerModal()">Trust Manager...</button>\\n </div>\\n'
                '<script src="./workspace_inbox.js"></script>",\n'
                '"summary": "Add Link & Resolve ambiguous email modal",\n'
                "}\n"
                "]\n"
            )
            parsed = bridge.parse_playground_markdown(sloppy_with_header)
            self.assertEqual(len(parsed["actions"]), 1)
            act = parsed["actions"][0]
            self.assertEqual(act["tool"], "patch_literal")
            self.assertEqual(act["args"]["path"], "core/ui/web/views/workspace_inbox.html")
            self.assertIn('class="inbox-header-actions"', act["args"]["old_text"])
            self.assertIn('onclick="openTrustManagerModal()"', act["args"]["old_text"])
            self.assertIn('<script src="./workspace_inbox.js"></script>', act["args"]["new_text"])
            self.assertEqual(act["args"]["summary"], "Add Link & Resolve ambiguous email modal")

            # Same sloppy JSON WITHOUT the leading `json` line
            sloppy_bare = sloppy_with_header.split("\n", 1)[1]
            parsed_bare = bridge.parse_playground_markdown(sloppy_bare)
            self.assertEqual(len(parsed_bare["actions"]), 1)
            self.assertEqual(parsed_bare["actions"][0]["args"]["summary"], "Add Link & Resolve ambiguous email modal")

            # Prose before unfenced json + raw literal newlines + invalid regex \s escape
            prose_sloppy = (
                "Apply this fix to the template and script:\n\n"
                "json\n"
                "{\n"
                '"tool": "patch_literal",\n'
                '"path": "views/modal.html",\n'
                '"old_text": "<div class="box">\n'
                '  re.match("\\s+", text)\n'
                '</div>",\n'
                '"new_text": "<div class="box updated">\n'
                '  re.match("\\s+", text)\n'
                '</div>"\n'
                "}\n\n"
                "Let me know when done."
            )
            parsed_inline = bridge.parse_playground_markdown(prose_sloppy)
            self.assertEqual(len(parsed_inline["actions"]), 1)
            ia = parsed_inline["actions"][0]
            self.assertEqual(ia["tool"], "patch_literal")
            self.assertIn('class="box"', ia["args"]["old_text"])
            self.assertIn('re.match("\\s+", text)', ia["args"]["old_text"])
            self.assertIn('class="box updated"', ia["args"]["new_text"])

    def test_parses_unfenced_leading_python_header_as_propose_script(self) -> None:
        with _load_bridge_with_stubs():
            for mod_name in ("models", "services.gemini_client", "services.bridge"):
                sys.modules.pop(mod_name, None)
            bridge = importlib.import_module("services.bridge")

            py_paste = (
                "python\n"
                "# path: packer.py\n"
                "from pathlib import Path\n"
                "Path('out.md').write_text('hello', encoding='utf-8')\n"
            )
            parsed = bridge.parse_playground_markdown(py_paste)
            self.assertEqual(len(parsed["actions"]), 1)
            self.assertEqual(parsed["actions"][0]["tool"], "propose_script")
            self.assertEqual(parsed["actions"][0]["args"]["path"], "packer.py")

            # Plain English starting with "python\n" must NOT become a script
            english = "python\nplease check why this script fails"
            parsed_eng = bridge.parse_playground_markdown(english)
            self.assertEqual(parsed_eng["actions"], [])

    def test_gate_execution_catches_patch_validation_error_without_crashing(self) -> None:
        import asyncio
        import json as _json

        with _load_bridge_with_stubs(), tempfile.TemporaryDirectory() as tmp:
            fastapi_mod = pytypes.ModuleType("fastapi")
            fastapi_mod.APIRouter = lambda **_kw: SimpleNamespace(
                post=lambda *_a, **_k: (lambda fn: fn),
                get=lambda *_a, **_k: (lambda fn: fn),
            )
            fastapi_mod.HTTPException = Exception
            fastapi_mod.Query = lambda default=None: default
            fastapi_mod.Request = object

            for mod_name in ("models", "services.gemini_client", "services.bridge", "routes.chat"):
                sys.modules.pop(mod_name, None)

            with patch.dict(sys.modules, {"fastapi": fastapi_mod}, clear=False):
                route_chat = importlib.import_module("routes.chat")

                target = Path(tmp) / "core" / "system" / "imap_client.py"
                target.parent.mkdir(parents=True)
                target.write_text("STATUS = 'v1'\nOTHER = 'ok'\n", encoding="utf-8")

                class FakeGateDB:
                    def __init__(self) -> None:
                        self.messages = []
                        self.gates = []
                        self.patches = []
                        self.session = SimpleNamespace(
                            id="s1", project_id="p1", api_enabled=False, directives_sent=True
                        )
                        self.project = SimpleNamespace(
                            id="p1",
                            name="Demo",
                            root_path=tmp,
                            python_path=None,
                            directives_path=None,
                            backup_dir="backup",
                            post_patch_command=None,
                            ignore_patterns=[],
                            preset_commands=[],
                        )

                    def get_session(self, sid):
                        return self.session if sid == "s1" else None

                    def get_project(self, pid):
                        return self.project if pid == "p1" else None

                    def get_gate(self, gid):
                        for g in self.gates:
                            if g["id"] == gid:
                                return g
                        return None

                    def resolve_gate(self, gid, status, result=None):
                        for g in self.gates:
                            if g["id"] == gid:
                                g["status"] = status
                                g["result"] = result

                    def list_pending_gates(self, sid):
                        return [g for g in self.gates if g["status"] == "pending"]

                    def add_message(self, sid, role, text, **kw):
                        self.messages.append(SimpleNamespace(id=f"m{len(self.messages)+1}", role=role, text=text, **kw))

                    def list_messages(self, sid):
                        return list(self.messages)

                    def add_patch_record(self, **kw):
                        rec = SimpleNamespace(model_dump=lambda: dict(kw))
                        self.patches.append(rec)
                        return rec

                db = FakeGateDB()
                # Gate 1 updates STATUS='v1' -> 'v2'; Gate 2 still expects STATUS='v1' (stale batch patch!)
                db.gates = [
                    {
                        "id": "g1",
                        "session_id": "s1",
                        "tool_name": "patch_literal",
                        "args": {
                            "path": "core/system/imap_client.py",
                            "old_text": "STATUS = 'v1'",
                            "new_text": "STATUS = 'v2'",
                        },
                        "preview": {"summary": "Patch 1", "call_id": "bridge-0"},
                        "status": "pending",
                    },
                    {
                        "id": "g2",
                        "session_id": "s1",
                        "tool_name": "patch_literal",
                        "args": {
                            "path": "core/system/imap_client.py",
                            "old_text": "STATUS = 'v1'\nOTHER = 'ok'",
                            "new_text": "STATUS = 'v1'\nOTHER = 'fixed'",
                        },
                        "preview": {"summary": "Patch 2", "call_id": "bridge-1"},
                        "status": "pending",
                    },
                ]
                req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=None)))
                batch_payload = SimpleNamespace(
                    session_id="s1",
                    decisions=[
                        SimpleNamespace(gate_id="g1", decision=SimpleNamespace(action="approve", reason=None)),
                        SimpleNamespace(gate_id="g2", decision=SimpleNamespace(action="approve", reason=None)),
                    ],
                )
                res = asyncio.run(route_chat.resolve_tool_gates_batch(batch_payload, req))
                self.assertEqual(res["pending_gates"], [])
                self.assertEqual(db.gates[0]["status"], "approved")
                self.assertEqual(db.gates[1]["status"], "error")
                self.assertEqual(len(res["gate_errors"]), 1)
                self.assertIn("Target literal block not found in 'core/system/imap_client.py'", res["gate_errors"][0])
                self.assertIn("Patch applied to `core/system/imap_client.py`", res["bridge_reply_text"])
                self.assertIn("Target literal block not found in 'core/system/imap_client.py'", res["bridge_reply_text"])
                self.assertIn("Target literal block not found in 'core/system/imap_client.py'", res["error_reply_text"])
                err_ev = _json.loads(db.messages[-1].text)
                self.assertEqual(err_ev["response"]["target"], "core/system/imap_client.py")

                # Single-gate approval failure while another gate remains pending also records tool_event + error_reply_text
                db2 = FakeGateDB()
                db2.gates = [
                    {
                        "id": "sg1",
                        "session_id": "s1",
                        "tool_name": "patch_literal",
                        "args": {
                            "path": "core/system/imap_client.py",
                            "old_text": "NONEXISTENT_BLOCK",
                            "new_text": "X",
                        },
                        "preview": {"summary": "Bad patch", "call_id": "bridge-0"},
                        "status": "pending",
                    },
                    {
                        "id": "sg2",
                        "session_id": "s1",
                        "tool_name": "patch_literal",
                        "args": {
                            "path": "core/system/imap_client.py",
                            "old_text": "OTHER = 'ok'",
                            "new_text": "OTHER = 'done'",
                        },
                        "preview": {"summary": "Other patch", "call_id": "bridge-1"},
                        "status": "pending",
                    },
                ]
                req2 = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db2, gemini=None)))
                single_res = asyncio.run(
                    route_chat.resolve_tool_gate("sg1", SimpleNamespace(action="approve", reason=None), req2)
                )
                self.assertEqual(len(single_res["pending_gates"]), 1)
                self.assertEqual(db2.gates[0]["status"], "error")
                self.assertEqual(len(single_res["gate_errors"]), 1)
                self.assertIn("Target literal block not found", single_res["error_reply_text"])





class TestRateTelemetryAndAppMode(unittest.TestCase):
    _cls_saved_mods: dict[str, Any] = {}
    _cls_ctx: Any = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        has_pydantic = importlib.util.find_spec("pydantic") is not None
        target_keys = (
            "models",
            "storage.db",
            "services.checkpoints",
            "services.bridge",
            "services.runner",
            "services.gemini_client",
            "routes.chat",
            "routes.workspace",
            "routes.bridge",
            "fastapi",
        )
        cls._cls_saved_mods = {k: sys.modules.get(k) for k in target_keys}
        cls._cls_ctx = patch.dict(sys.modules, {}, clear=False) if has_pydantic else _load_bridge_with_stubs()
        cls._cls_ctx.__enter__()
        fastapi_mod = pytypes.ModuleType("fastapi")
        fastapi_mod.APIRouter = lambda **_kw: SimpleNamespace(
            post=lambda *_a, **_k: (lambda fn: fn),
            get=lambda *_a, **_k: (lambda fn: fn),
        )
        fastapi_mod.HTTPException = Exception
        fastapi_mod.Query = lambda default=None, **_kw: default
        fastapi_mod.Request = object
        for k in target_keys:
            if k != "fastapi":
                sys.modules.pop(k, None)
        sys.modules["fastapi"] = fastapi_mod

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            for k, orig in cls._cls_saved_mods.items():
                if orig is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = orig
            cls._cls_saved_mods.clear()
        finally:
            if cls._cls_ctx is not None:
                cls._cls_ctx.__exit__(None, None, None)
                cls._cls_ctx = None
            super().tearDownClass()

    def test_rpm_tpm_decay_and_pacific_midnight_rpd_reset(self) -> None:
        from models import TokenStats
        from storage.db import CompanionDB

        if True:
            if True:

                with tempfile.TemporaryDirectory() as tmp:
                    db_path = Path(tmp) / "telemetry.db"
                    # Seed a legacy DB to verify migrations run cleanly
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

                    # Insert a legacy session row BEFORE DB migration checks
                    conn = sqlite3.connect(str(db_path))
                    conn.execute(
                        "INSERT INTO projects (id, name, root_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        ("p-legacy", "LegacyProj", tmp, "2026-01-01", "2026-01-01"),
                    )
                    conn.execute(
                        "INSERT INTO sessions (id, project_id, title, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        ("s-legacy", "p-legacy", "Existing API Session", "gemini-4-hydrogen", "2026-01-01", "2026-01-01"),
                    )
                    conn.commit()
                    conn.close()

                    db = CompanionDB(db_path)
                    cols = {
                        r[1]
                        for r in sqlite3.connect(str(db_path)).execute("PRAGMA table_info(sessions)").fetchall()
                    }
                    self.assertIn("directives_sent", cols)
                    self.assertIn("last_context_tokens", cols)
                    self.assertIn("api_enabled", cols)

                    # Existing pre-Bridge sessions MUST migrate as API sessions (api_enabled=True)
                    legacy_sess = db.get_session("s-legacy")
                    self.assertIsNotNone(legacy_sess)
                    assert legacy_sess is not None
                    self.assertTrue(legacy_sess.api_enabled)

                    # New default sessions default to Playground Bridge (api_enabled=False)
                    from models import SessionCreate
                    bridge_sess = db.create_session(SessionCreate(project_id="p-legacy", title="Bridge 1"))
                    self.assertFalse(bridge_sess.api_enabled)

                    # 2026-11-16 23:59:30 PST is 2026-11-17 07:59:30 UTC
                    t_before_midnight = datetime(2026, 11, 17, 7, 59, 30, tzinfo=timezone.utc).timestamp()
                    db.record_api_usage("gemini-4-hydrogen", TokenStats(input_tokens=4000, output_tokens=50, thinking_tokens=0), created_ts=t_before_midnight - 10)
                    db.record_api_usage("gemini-4-hydrogen", TokenStats(input_tokens=6000, output_tokens=80, thinking_tokens=0), created_ts=t_before_midnight)

                    # Immediately at t_before_midnight: RPM=2, TPM=10000, RPD=2
                    snap1 = db.get_rate_telemetry("gemini-4-hydrogen", now_ts=t_before_midnight)
                    self.assertEqual(snap1["rpm"], 2)
                    self.assertEqual(snap1["tpm"], 10000)
                    self.assertEqual(snap1["rpd"], 2)

                    # 61s later (08:00:31 UTC -> 00:00:31 PST next day!): minute window empty AND RPD reset!
                    t_after_midnight = t_before_midnight + 61.0
                    snap2 = db.get_rate_telemetry("gemini-4-hydrogen", now_ts=t_after_midnight)
                    self.assertEqual(snap2["rpm"], 0)
                    self.assertEqual(snap2["tpm"], 0)
                    self.assertEqual(snap2["rpd"], 0)
        if False:
            pass

    def test_atomic_checkpoints_and_gated_run_tests(self) -> None:
        import asyncio
        from models import ProjectCreate, SessionCreate
        from routes import chat as route_chat
        from routes import workspace as route_workspace
        from storage.db import CompanionDB

        if True:
            if True:
                if True:

                    with tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp) / "proj"
                        root.mkdir()
                        (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
                        (root / "untouched.py").write_text("KEEP_ME = 123\n", encoding="utf-8")

                        db = CompanionDB(Path(tmp) / "companion.db")
                        proj = db.create_project(
                            ProjectCreate(
                                name="AtomicProj",
                                root_path=str(root),
                                python_path=None,
                                directives_path=None,
                                backup_dir="backup",
                                ignore_patterns=[],
                                preset_commands=[],
                                post_patch_command='python3 -c "import app; assert app.VALUE == 2"',
                            )
                        )
                        sess = db.create_session(
                            SessionCreate(
                                project_id=proj.id,
                                title="S1",
                                api_enabled=False,
                                model="gemini-4-hydrogen",
                                thinking_level="HIGH",
                                auto_approve_read_tokens=0,
                            )
                        )
                        db.add_message(sess.id, role="user", text="Update VALUE to 2 and test")
                        db.add_message(sess.id, role="model", text="Patching now")
                        expected_anchor = len(db.list_messages(sess.id))

                        # Batch 1: failed test run keeps checkpoint active; fixing & running gated run_tests commits!
                        g_patch = db.create_gate(
                            sess.id,
                            "patch_literal",
                            {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 99"},
                            {"summary": "Wrong value"},
                        )
                        g_new = db.create_gate(
                            sess.id,
                            "write_file",
                            {"path": "extra.py", "content": "FLAG = True\n"},
                            {"summary": "New file"},
                        )
                        g_test_fail = db.create_gate(
                            sess.id,
                            "run_tests",
                            {"summary": "Verify VALUE"},
                            {"summary": "Verify VALUE", "command": proj.post_patch_command},
                        )

                        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=None)))
                        res1 = asyncio.run(
                            route_chat.resolve_tool_gates_batch(
                                SimpleNamespace(
                                    session_id=sess.id,
                                    decisions=[
                                        SimpleNamespace(gate_id=g_patch["id"], decision=SimpleNamespace(action="approve", reason=None)),
                                        SimpleNamespace(gate_id=g_new["id"], decision=SimpleNamespace(action="approve", reason=None)),
                                        SimpleNamespace(gate_id=g_test_fail["id"], decision=SimpleNamespace(action="approve", reason=None)),
                                    ],
                                ),
                                req,
                            )
                        )
                        self.assertTrue((root / "extra.py").is_file())
                        self.assertEqual(len(res1["checkpoints"]), 1)
                        cp1 = res1["checkpoints"][0]
                        self.assertEqual(cp1.status, "active")
                        self.assertEqual(cp1.after_msg_index, expected_anchor)
                        self.assertIn("app.py", cp1.modified_files)
                        self.assertIn("extra.py", cp1.created_files)
                        # CoW Savepoint: untouched files are NOT copied, modified files ARE captured
                        self.assertFalse((Path(cp1.snapshot_dir) / "files" / "untouched.py").exists())
                        self.assertTrue((Path(cp1.snapshot_dir) / "files" / "app.py").is_file())
                        self.assertIn("FAILED", res1["bridge_reply_text"])

                        # Test Restore rollback: reverts app.py to VALUE = 1 and removes extra.py!
                        g_pending_cancelled = db.create_gate(
                            sess.id,
                            "patch_literal",
                            {"path": "app.py", "old_text": "VALUE = 99", "new_text": "VALUE = 2"},
                            {"summary": "Should cancel on restore"},
                        )
                        restore_res = route_workspace.restore_project_checkpoint(cp1.id, req)
                        self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "VALUE = 1\n")
                        self.assertFalse((root / "extra.py").exists())
                        self.assertEqual(restore_res["checkpoint"].status, "restored")
                        self.assertEqual(restore_res["pending_gates"], [])
                        self.assertEqual(restore_res["cancelled_gates"], 1)
                        self.assertEqual(db.get_gate(g_pending_cancelled["id"])["status"], "cancelled")

                        # Batch 2: correct patch + gated run_tests pass -> auto-commits checkpoint!
                        g_fix = db.create_gate(
                            sess.id,
                            "patch_literal",
                            {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
                            {"summary": "Set VALUE=2"},
                        )
                        g_test_pass = db.create_gate(
                            sess.id,
                            "run_tests",
                            {"summary": "Verify VALUE=2"},
                            {"summary": "Verify VALUE=2", "command": proj.post_patch_command},
                        )
                        res2 = asyncio.run(
                            route_chat.resolve_tool_gates_batch(
                                SimpleNamespace(
                                    session_id=sess.id,
                                    decisions=[
                                        SimpleNamespace(gate_id=g_fix["id"], decision=SimpleNamespace(action="approve", reason=None)),
                                        SimpleNamespace(gate_id=g_test_pass["id"], decision=SimpleNamespace(action="approve", reason=None)),
                                    ],
                                ),
                                req,
                            )
                        )
                        self.assertTrue(res2.get("checkpoint_committed"))
                        self.assertEqual(len(res2["checkpoints"]), 2)
                        self.assertEqual(res2["checkpoints"][1].status, "committed")
                        self.assertFalse(Path(res2["checkpoints"][1].snapshot_dir).exists())
                        self.assertIn("PASSED", res2["bridge_reply_text"])

                        # Batch 3: manual Commit Anyway on active checkpoint
                        g_manual = db.create_gate(
                            sess.id,
                            "write_file",
                            {"path": "notes.txt", "content": "hi\n"},
                            {"summary": "Notes"},
                        )
                        res3 = asyncio.run(
                            route_chat.resolve_tool_gate(
                                g_manual["id"], SimpleNamespace(action="approve", reason=None), req
                            )
                        )
                        cp3 = res3["checkpoints"][2]
                        self.assertEqual(cp3.status, "active")
                        comm_res = route_workspace.commit_project_checkpoint(cp3.id, req)
                        self.assertEqual(comm_res["checkpoint"].status, "committed")

        if False:
            pass

    def test_gated_delete_rename_and_fast_target_run_tests(self) -> None:
        import asyncio
        from models import ProjectCreate, SessionCreate
        from routes import chat as route_chat
        from routes import workspace as route_workspace
        from services import bridge, runner
        from storage.db import CompanionDB

        if True:
            if True:
                if True:

                    with tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp) / "proj"
                        root.mkdir()
                        (root / "old_mod.py").write_text("X = 10\n", encoding="utf-8")
                        (root / "obsolete.txt").write_text("remove me\n", encoding="utf-8")
                        (root / "styles.css").write_text("body { color: #fff; }\n", encoding="utf-8")
                        (root / "view.html").write_text("<div>ok</div>\n", encoding="utf-8")

                        db = CompanionDB(Path(tmp) / "companion.db")
                        proj = db.create_project(
                            ProjectCreate(
                                name="OpsProj",
                                root_path=str(root),
                                python_path=None,
                                directives_path=None,
                                backup_dir="backup",
                                ignore_patterns=[],
                                preset_commands=[],
                                post_patch_command='python3 -c "print(\'full-ok\')"',
                                fast_test_command='python3 -c "import sys; print(\'fast:\' + sys.argv[1])" {target}',
                            )
                        )
                        # Verify runner.resolve_test_command routing & is_full_suite flags
                        full_info = runner.resolve_test_command(proj)
                        self.assertEqual(full_info["command"], proj.post_patch_command)
                        self.assertTrue(full_info["is_full_suite"])

                        fast_css = runner.resolve_test_command(proj, fast=True, target="styles.css")
                        self.assertTrue(fast_css["command"].endswith("styles.css"))
                        self.assertFalse(fast_css["is_full_suite"])

                        # Project without fast_test_command uses safe built-in target checks
                        proj_no_fast = (
                            proj.model_copy(update={"fast_test_command": None})
                            if hasattr(proj, "model_copy")
                            else SimpleNamespace(**{**proj.__dict__, "fast_test_command": None})
                        )
                        py_chk = runner.resolve_test_command(proj_no_fast, fast=True, target="old_mod.py")
                        self.assertIn("py_compile", py_chk["command"])
                        self.assertFalse(py_chk["is_full_suite"])
                        css_chk = runner.resolve_test_command(proj_no_fast, fast=True, target="styles.css")
                        self.assertIn("styles.css", css_chk["command"])
                        self.assertFalse(css_chk["is_full_suite"])
                        html_chk = runner.resolve_test_command(proj_no_fast, fast=True, target="view.html")
                        self.assertIn("HTMLParser", html_chk["command"])
                        self.assertFalse(html_chk["is_full_suite"])

                        sess = db.create_session(
                            SessionCreate(
                                project_id=proj.id,
                                title="S-ops",
                                api_enabled=False,
                                model="gemini-4-hydrogen",
                                thinking_level="HIGH",
                                auto_approve_read_tokens=0,
                            )
                        )
                        db.add_message(sess.id, role="user", text="Rename, delete, and quick-check")

                        raw = (
                            "Refactoring old_mod.py and removing obsolete.txt, then running a quick check.\n"
                            "```json\n"
                            "[\n"
                            '  {"tool": "rename_file", "old_path": "old_mod.py", "new_path": "new_mod.py"},\n'
                            '  {"tool": "delete_file", "path": "obsolete.txt"},\n'
                            '  {"tool": "run_tests", "fast": true, "target": "new_mod.py", "summary": "Quick check new_mod"}\n'
                            "]\n"
                            "```\n"
                            "Wait for output\n"
                            "## Local Workspace Tool Results\n"
                            "Hallucinated output"
                        )
                        parsed = bridge.parse_playground_markdown(raw)
                        self.assertEqual(len(parsed["actions"]), 3)
                        self.assertNotIn("Wait for output", parsed["prose_text"])

                        g_rename = db.create_gate(
                            sess.id,
                            "rename_file",
                            {"old_path": "old_mod.py", "new_path": "new_mod.py"},
                            {"summary": "Rename old_mod.py -> new_mod.py"},
                        )
                        g_del = db.create_gate(
                            sess.id,
                            "delete_file",
                            {"path": "obsolete.txt"},
                            {"summary": "Delete obsolete.txt"},
                        )
                        g_fast = db.create_gate(
                            sess.id,
                            "run_tests",
                            {"fast": True, "target": "new_mod.py", "summary": "Quick check"},
                            {"summary": "Quick check new_mod", "command": "python3 -m py_compile new_mod.py", "is_full_suite": False},
                        )

                        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=None)))
                        res = asyncio.run(
                            route_chat.resolve_tool_gates_batch(
                                SimpleNamespace(
                                    session_id=sess.id,
                                    decisions=[
                                        SimpleNamespace(gate_id=g_rename["id"], decision=SimpleNamespace(action="approve", reason=None)),
                                        SimpleNamespace(gate_id=g_del["id"], decision=SimpleNamespace(action="approve", reason=None)),
                                        SimpleNamespace(gate_id=g_fast["id"], decision=SimpleNamespace(action="approve", reason=None)),
                                    ],
                                ),
                                req,
                            )
                        )
                        self.assertFalse((root / "old_mod.py").exists())
                        self.assertTrue((root / "new_mod.py").is_file())
                        self.assertFalse((root / "obsolete.txt").exists())
                        # Crucial invariant: fast/targeted check MUST NOT auto-commit active checkpoint
                        self.assertFalse(res.get("checkpoint_committed"))
                        self.assertEqual(len(res["checkpoints"]), 1)
                        cp = res["checkpoints"][0]
                        self.assertEqual(cp.status, "active")
                        self.assertIn("old_mod.py", cp.modified_files)
                        self.assertIn("obsolete.txt", cp.modified_files)
                        self.assertIn("new_mod.py", cp.created_files)
                        self.assertIn("quick/targeted check", res["bridge_reply_text"])
                        self.assertIn("fast:new_mod.py", res["bridge_reply_text"])
                        self.assertIn("Renamed", res["bridge_reply_text"])
                        self.assertIn("Deleted", res["bridge_reply_text"])

                        # Restore checkpoint -> restores old_mod.py + obsolete.txt and removes new_mod.py
                        restore_res = route_workspace.restore_project_checkpoint(cp.id, req)
                        self.assertEqual(restore_res["checkpoint"].status, "restored")
                        self.assertTrue((root / "old_mod.py").is_file())
                        self.assertEqual((root / "old_mod.py").read_text(encoding="utf-8"), "X = 10\n")
                        self.assertTrue((root / "obsolete.txt").is_file())
                        self.assertEqual((root / "obsolete.txt").read_text(encoding="utf-8"), "remove me\n")
                        self.assertFalse((root / "new_mod.py").exists())
        if False:
            pass

    def test_propose_script_exit0_output_run_script_and_live_streaming(self) -> None:
        import asyncio
        import json as _json
        from models import ProjectCreate, SessionCreate
        from routes import chat as route_chat
        from services import bridge, gemini_client
        from storage.db import CompanionDB

        if True:
            if True:
                if True:

                    with tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp) / "proj"
                        root.mkdir()
                        db = CompanionDB(Path(tmp) / "companion.db")
                        proj = db.create_project(
                            ProjectCreate(
                                name="ScriptProj",
                                root_path=str(root),
                                python_path=sys.executable,
                                directives_path=None,
                                backup_dir="backup",
                                ignore_patterns=[],
                                preset_commands=[],
                            )
                        )
                        sess = db.create_session(
                            SessionCreate(
                                project_id=proj.id,
                                title="S-script",
                                api_enabled=False,
                                model="gemini-4-hydrogen",
                                thinking_level="HIGH",
                                auto_approve_read_tokens=0,
                            )
                        )

                        ws_events: list[dict] = []

                        class FakeWS:
                            async def send_text(self, raw: str) -> None:
                                ws_events.append(_json.loads(raw))

                        app_obj = SimpleNamespace(state=SimpleNamespace(db=db, gemini=None, lifecycle_sockets={FakeWS()}))
                        req = SimpleNamespace(app=app_obj)

                        # 1. propose_script exiting cleanly (exit 0) MUST include full stdout in bridge_reply_text AND stream over WebSocket
                        script_code = (
                            "import sys\n"
                            "print('HEADER_LINE')\n"
                            "sys.stdout.flush()\n"
                            "print('PROBE_RESULT_42', ' '.join(sys.argv[1:]))\n"
                        )
                        g_prop = db.create_gate(
                            sess.id,
                            "propose_script",
                            {"path": "probe.py", "content": script_code, "summary": "Run probe"},
                            {"summary": "Run probe"},
                        )
                        res1 = asyncio.run(
                            route_chat.resolve_tool_gate(
                                g_prop["id"], SimpleNamespace(action="write_and_run", reason=None), req
                            )
                        )
                        self.assertIn("HEADER_LINE", res1["bridge_reply_text"])
                        self.assertIn("PROBE_RESULT_42", res1["bridge_reply_text"])
                        self.assertIn("exit code `0`", res1["bridge_reply_text"])
                        self.assertTrue(any(ev.get("type") == "exec_start" for ev in ws_events))
                        self.assertTrue(any(ev.get("type") == "exec_chunk" and "PROBE_RESULT_42" in ev.get("chunk", "") for ev in ws_events))
                        self.assertTrue(any(ev.get("type") == "exec_done" and ev.get("exit_code") == 0 for ev in ws_events))

                        # 2. run_script executes patched/existing script with optional args behind a human gate
                        parsed = bridge.parse_playground_markdown(
                            'Re-running patched probe script.\n```json\n{"tool": "run_script", "path": "probe.py", "args": "--verbose demo", "summary": "Re-run probe"}\n```'
                        )
                        self.assertEqual(parsed["actions"][0]["tool"], "run_script")
                        preview = gemini_client.prepare_tool_gate_preview(proj, "run_script", parsed["actions"][0]["args"])
                        self.assertEqual(preview["category"], "command")
                        self.assertEqual(preview["script_path"], "probe.py")

                        g_run = db.create_gate(
                            sess.id,
                            "run_script",
                            parsed["actions"][0]["args"],
                            {k: v for k, v in preview.items() if k != "prepared_result"},
                        )
                        res2 = asyncio.run(
                            route_chat.resolve_tool_gate(
                                g_run["id"], SimpleNamespace(action="approve", reason=None), req
                            )
                        )
                        self.assertIn("**run_script**", res2["bridge_reply_text"])
                        self.assertIn("PROBE_RESULT_42 --verbose demo", res2["bridge_reply_text"])
                        self.assertEqual(res2["command_results"][0]["exit_code"], 0)

                        # 3. run_and_delete_script keeps failing script for patching, then auto-deletes once patched & passing
                        probe2 = root / "inspect_tmp.py"
                        probe2.write_text("import sys\nprint('ABOUT_TO_FAIL')\nsys.exit(2)\n", encoding="utf-8")
                        parsed_fail = bridge.parse_playground_markdown(
                            'Running inspection probe.\n```json\n{"tool": "run_and_delete_script", "path": "inspect_tmp.py", "summary": "Inspect state"}\n```'
                        )
                        self.assertEqual(parsed_fail["actions"][0]["tool"], "run_and_delete_script")
                        prev_fail = gemini_client.prepare_tool_gate_preview(
                            proj, "run_and_delete_script", parsed_fail["actions"][0]["args"]
                        )
                        self.assertTrue(prev_fail["auto_delete_on_success"])
                        g_fail = db.create_gate(
                            sess.id,
                            "run_and_delete_script",
                            parsed_fail["actions"][0]["args"],
                            {k: v for k, v in prev_fail.items() if k != "prepared_result"},
                        )
                        db.add_message(sess.id, role="model", text="Running probe2")
                        res_fail = asyncio.run(
                            route_chat.resolve_tool_gate(
                                g_fail["id"], SimpleNamespace(action="approve", reason=None), req
                            )
                        )
                        self.assertTrue(probe2.is_file(), "Failing script must stay on disk for patching")
                        self.assertIn("script kept on disk for patching", res_fail["bridge_reply_text"])

                        db.add_message(sess.id, role="model", text="Patched inspect_tmp.py")
                        probe2.write_text("print('INSPECTION_OK_99')\n", encoding="utf-8")
                        g_pass = db.create_gate(
                            sess.id,
                            "run_and_delete_script",
                            {"path": "inspect_tmp.py", "summary": "Re-run patched probe"},
                            {k: v for k, v in prev_fail.items() if k != "prepared_result"},
                        )
                        res_pass = asyncio.run(
                            route_chat.resolve_tool_gate(
                                g_pass["id"], SimpleNamespace(action="approve", reason=None), req
                            )
                        )
                        self.assertFalse(probe2.exists(), "Passing run_and_delete_script must delete the script")
                        self.assertIn("INSPECTION_OK_99", res_pass["bridge_reply_text"])
                        self.assertIn("script auto-deleted", res_pass["bridge_reply_text"])
                        self.assertIsNotNone(res_pass.get("patch_record"))

                        # 4. propose_script with action='write_run_and_delete' writes, executes, captures output, and deletes
                        g_oneshot = db.create_gate(
                            sess.id,
                            "propose_script",
                            {"path": "oneshot.py", "content": "print('ONESHOT_42')\n", "delete_on_success": True},
                            {"summary": "One-shot check", "delete_on_success": True},
                        )
                        db.add_message(sess.id, role="model", text="One-shot check")
                        res_oneshot = asyncio.run(
                            route_chat.resolve_tool_gate(
                                g_oneshot["id"], SimpleNamespace(action="write_run_and_delete", reason=None), req
                            )
                        )
                        self.assertFalse((root / "oneshot.py").exists())
                        self.assertIn("ONESHOT_42", res_oneshot["bridge_reply_text"])
                        self.assertIn("deleted temporary script", res_oneshot["bridge_reply_text"])

                        # 5. Automatic DB pruning + compaction + VACUUM keeps companion.db lean
                        from models import AttachmentItem

                        big_blob = "line = 1\n" * 3500
                        big_img = "data:image/png;base64," + ("A" * 20000)
                        for idx in range(30):
                            atts = (
                                [AttachmentItem(kind="image", label=f"p{idx}.png", content=big_img, mime_type="image/png", estimated_tokens=400)]
                                if idx < 4
                                else []
                            )
                            db.add_message(sess.id, role="user", text=f"Prompt {idx}", attachments=atts)
                            db.add_message(
                                sess.id,
                                role="model",
                                text=f"Reply {idx}",
                                tool_calls=[{"id": f"k{idx}", "name": "read_file", "args": {"path": "x.py"}}],
                            )
                            db.add_message(
                                sess.id,
                                role="tool_event",
                                text=_json.dumps({
                                    "tool_name": "read_file",
                                    "call_id": f"k{idx}",
                                    "response": {"path": "x.py", "total_lines": 3500, "content": big_blob},
                                }),
                            )

                        msgs_after = db.list_messages(sess.id)
                        self.assertLessEqual(len(msgs_after), 65)
                        self.assertNotEqual(msgs_after[0].role, "tool_event")
                        stats = db.cleanup_database(force_vacuum=True)
                        self.assertTrue(stats["vacuumed"])
                        self.assertLess(stats["db_bytes_after"], 250_000)
                        self.assertLess(stats["db_bytes_after"], stats["db_bytes_before"] // 4)

                        # 6. Clean propose_inspection_script discards temporary empty Savepoint; run_and_delete_script errors clearly or accepts inline content
                        sess_insp = db.create_session(
                            SessionCreate(project_id=proj.id, title="CleanInspect", api_enabled=False)
                        )
                        g_insp = db.create_gate(
                            sess_insp.id,
                            "propose_inspection_script",
                            {"path": "inspect_genai_model.py", "content": "print('CLEAN_INSPECT')\n", "summary": "Check model"},
                            {"summary": "Check model", "delete_on_success": True},
                        )
                        res_insp = asyncio.run(
                            route_chat.resolve_tool_gate(
                                g_insp["id"], SimpleNamespace(action="write_run_and_delete", reason=None), req
                            )
                        )
                        self.assertFalse((root / "inspect_genai_model.py").exists())
                        self.assertIn("CLEAN_INSPECT", res_insp["bridge_reply_text"])
                        self.assertEqual(db.list_checkpoints(sess_insp.id), [])

                        g_missing = db.create_gate(
                            sess_insp.id,
                            "run_and_delete_script",
                            {"path": "check_genai_schema.py"},
                            {"summary": "Missing script"},
                        )
                        res_missing = asyncio.run(
                            route_chat.resolve_tool_gate(
                                g_missing["id"], SimpleNamespace(action="approve", reason=None), req
                            )
                        )
                        self.assertTrue(res_missing.get("gate_error"))
                        self.assertIn("propose_inspection_script", res_missing["gate_error"])

                        g_inline = db.create_gate(
                            sess_insp.id,
                            "run_and_delete_script",
                            {"path": "check_genai_schema.py", "content": "print('SCHEMA_INLINE_OK')\n"},
                            {"summary": "Inline schema check", "delete_on_success": True},
                        )
                        res_inline = asyncio.run(
                            route_chat.resolve_tool_gate(
                                g_inline["id"], SimpleNamespace(action="write_run_and_delete", reason=None), req
                            )
                        )
                        self.assertFalse((root / "check_genai_schema.py").exists())
                        self.assertIn("SCHEMA_INLINE_OK", res_inline["bridge_reply_text"])
                        self.assertEqual(db.list_checkpoints(sess_insp.id), [])
        if False:
            pass

    def test_parses_raw_patch_and_tagged_inspection_scripts_without_json(self) -> None:
        with _load_bridge_with_stubs():
            for mod_name in ("models", "services.gemini_client", "services.bridge"):
                sys.modules.pop(mod_name, None)
            bridge = importlib.import_module("services.bridge")

            # 1. Raw ```patch block with quotes, backslashes, and indentation (no JSON escaping)
            raw_patch_md = (
                "Step 1/2: Updating parser & regex rule cleanly.\n\n"
                "```patch\n"
                "# path: app/parser.py\n"
                "# summary: Update pattern and greeting\n"
                "<<<<<<< SEARCH\n"
                "def greet(name):\n"
                '    pat = r"\\s+"\n'
                '    return "hi " + name\n'
                "=======\n"
                "def greet(name):\n"
                '    pat = r"\\w+"\n'
                '    return f"hello {name}"\n'
                ">>>>>>> REPLACE\n"
                "<<<<<<< SEARCH\n"
                "FLAG = False\n"
                "=======\n"
                "FLAG = True\n"
                ">>>>>>> REPLACE\n"
                "```\n\n"
                "```patch\n"
                "# path: web/index.html\n"
                "<<<<<<< SEARCH\n"
                '<div class="old">Hi</div>\n'
                "=======\n"
                '<div class="new">Hello</div>\n'
                ">>>>>>> REPLACE\n"
                "```\n"
                "Wait for output.\n"
                "## Local Workspace Tool Results\n"
                "fake hallucination\n"
            )
            p_patch = bridge.parse_playground_markdown(raw_patch_md)
            self.assertEqual([a["tool"] for a in p_patch["actions"]], ["patch_literal", "patch_literal", "patch_literal"])
            self.assertEqual(p_patch["actions"][0]["args"]["path"], "app/parser.py")
            self.assertIn('pat = r"\\s+"', p_patch["actions"][0]["args"]["old_text"])
            self.assertIn('return f"hello {name}"', p_patch["actions"][0]["args"]["new_text"])
            self.assertEqual(p_patch["actions"][1]["args"]["old_text"], "FLAG = False")
            self.assertEqual(p_patch["actions"][2]["args"]["path"], "web/index.html")
            self.assertNotIn("Wait for output", p_patch["prose_text"])

            # 2. Raw ```python inspection script with directives
            raw_script_md = (
                "Inspecting SDK schema.\n\n"
                "```python\n"
                "# tool: propose_inspection_script\n"
                "# path: inspect_sdk.py\n"
                "# summary: Print installed schema fields\n"
                "import sys\n"
                'print("OK\\nline2")\n'
                "```\n"
            )
            p_scr = bridge.parse_playground_markdown(raw_script_md)
            self.assertEqual(len(p_scr["actions"]), 1)
            self.assertEqual(p_scr["actions"][0]["tool"], "propose_inspection_script")
            self.assertEqual(p_scr["actions"][0]["args"]["path"], "inspect_sdk.py")
            self.assertEqual(p_scr["actions"][0]["args"]["summary"], "Print installed schema fields")
            self.assertNotIn("# tool:", p_scr["actions"][0]["args"]["content"])
            self.assertIn('print("OK\\nline2")', p_scr["actions"][0]["args"]["content"])

            # 3. Tagged write_file in JS + hybrid JSON header paired with following code block
            raw_js_md = (
                "```javascript\n"
                "// tool: write_file\n"
                "// path: web/app.js\n"
                "// summary: Create helper module\n"
                "export const ok = true;\n"
                "```\n"
            )
            p_js = bridge.parse_playground_markdown(raw_js_md)
            self.assertEqual(p_js["actions"][0]["tool"], "write_file")
            self.assertEqual(p_js["actions"][0]["args"]["path"], "web/app.js")
            self.assertEqual(p_js["actions"][0]["args"]["content"], "export const ok = true;")

            hybrid_md = (
                "Running inspector:\n"
                '```json\n{"tool": "propose_inspection_script", "path": "check_env.py", "summary": "Verify env"}\n```\n'
                "```python\nimport os\nprint(os.name)\n```\n"
            )
            p_hyb = bridge.parse_playground_markdown(hybrid_md)
            self.assertEqual(len(p_hyb["actions"]), 1)
            self.assertEqual(p_hyb["actions"][0]["tool"], "propose_inspection_script")
            self.assertEqual(p_hyb["actions"][0]["args"]["path"], "check_env.py")
            self.assertIn("import os", p_hyb["actions"][0]["args"]["content"])

    def test_parses_unfenced_python_and_patch_with_intent_classification_and_tail_trimming(self) -> None:
        with _load_bridge_with_stubs():
            for mod_name in ("models", "services.gemini_client", "services.bridge"):
                sys.modules.pop(mod_name, None)
            bridge = importlib.import_module("services.bridge")

            # 1. Explicit inspection 1-liner + fenced python (no # tool: comments) -> unambiguous inspection
            insp_md = (
                "Step 1/2 — Inspection script (inspect_db.py): Check SQLite schema\n\n"
                "```python\n"
                "import sqlite3\n"
                "print(sqlite3.sqlite_version)\n"
                "```\n"
                "Output:\nFake output hallucination\n"
            )
            p_insp = bridge.parse_playground_markdown(insp_md)
            self.assertEqual(len(p_insp["actions"]), 1)
            self.assertEqual(p_insp["actions"][0]["tool"], "propose_inspection_script")
            self.assertEqual(p_insp["actions"][0]["args"]["path"], "inspect_db.py")
            self.assertFalse(bool(p_insp["actions"][0]["args"].get("intent_ambiguous")))
            self.assertNotIn("Fake output hallucination", p_insp["prose_text"])

            # 2. Explicit new file 1-liner + fenced python (no # tool: comments) -> unambiguous write_file
            new_md = (
                "Step 2/2 — New file (utils/math_helpers.py): Add clamp helper\n\n"
                "```python\n"
                "def clamp(v: int, lo: int, hi: int) -> int:\n"
                "    return max(lo, min(hi, v))\n"
                "```\n"
            )
            p_new = bridge.parse_playground_markdown(new_md)
            self.assertEqual(len(p_new["actions"]), 1)
            self.assertEqual(p_new["actions"][0]["tool"], "write_file")
            self.assertEqual(p_new["actions"][0]["args"]["path"], "utils/math_helpers.py")
            self.assertIn("def clamp", p_new["actions"][0]["args"]["content"])

            # 3. Ambiguous 1-liner + fenced python -> intent_ambiguous=True
            amb_md = (
                "Here is the script:\n\n"
                "```python\n"
                "import os\n"
                "print(os.getcwd())\n"
                "```\n"
            )
            p_amb = bridge.parse_playground_markdown(amb_md)
            self.assertEqual(len(p_amb["actions"]), 1)
            self.assertEqual(p_amb["actions"][0]["tool"], "propose_inspection_script")
            self.assertTrue(p_amb["actions"][0]["args"].get("intent_ambiguous"))

            # 4. Unfenced one-liner + Python script with hallucinated trailing prose/Output
            unfenced_py = (
                "Inspection script (check_layout.py): Verify files\n"
                "python\n"
                "from pathlib import Path\n"
                "print(len(list(Path('.').iterdir())))\n"
                "Output:\n42\n"
                "## Local Workspace Tool Results\nfake"
            )
            p_unpy = bridge.parse_playground_markdown(unfenced_py)
            self.assertEqual(len(p_unpy["actions"]), 1)
            self.assertEqual(p_unpy["actions"][0]["tool"], "propose_inspection_script")
            self.assertEqual(p_unpy["actions"][0]["args"]["path"], "check_layout.py")
            self.assertNotIn("Output:", p_unpy["actions"][0]["args"]["content"])
            self.assertNotIn("fake", p_unpy["prose_text"])

            # 5. Unfenced SEARCH/REPLACE patch + trailing hallucinated prose
            unfenced_patch = (
                "Patching (core/app.py): Enable debug flag\n"
                "# path: core/app.py\n"
                "<<<<<<< SEARCH\n"
                "DEBUG = False\n"
                "=======\n"
                "DEBUG = True\n"
                ">>>>>>> REPLACE\n"
                "Output:\nApplied cleanly!\n"
            )
            p_unpatch = bridge.parse_playground_markdown(unfenced_patch)
            self.assertEqual(len(p_unpatch["actions"]), 1)
            self.assertEqual(p_unpatch["actions"][0]["tool"], "patch_literal")
            self.assertEqual(p_unpatch["actions"][0]["args"]["path"], "core/app.py")
            self.assertEqual(p_unpatch["actions"][0]["args"]["old_text"], "DEBUG = False")
            self.assertEqual(p_unpatch["actions"][0]["args"]["new_text"], "DEBUG = True")
            self.assertNotIn("Applied cleanly", p_unpatch["prose_text"])

            # 6. Raw ```patch with # lines: START-END + # expect_start / # expect_end (no <<< markers)
            raw_line_patch = (
                "Patching (core/client.py): Rewrite retry loop cleanly\n"
                "```patch\n"
                "# path: core/client.py\n"
                "# lines: 140-295\n"
                "# expect_start: def send_retry(payload):\n"
                "# expect_end: return finalize(resp)\n"
                "# summary: Rewrite retry loop\n"
                "def send_retry(payload):\n"
                "    return fast_send(payload)\n"
                "```\n"
                "Output:\nDone!\n"
            )
            p_line = bridge.parse_playground_markdown(raw_line_patch)
            self.assertEqual(len(p_line["actions"]), 1)
            self.assertEqual(p_line["actions"][0]["tool"], "patch_lines")
            la = p_line["actions"][0]["args"]
            self.assertEqual(la["path"], "core/client.py")
            self.assertEqual(la["start_line"], 140)
            self.assertEqual(la["end_line"], 295)
            self.assertEqual(la["expect_start"], "def send_retry(payload):")
            self.assertEqual(la["expect_end"], "return finalize(resp)")
            self.assertIn("return fast_send(payload)", la["new_text"])
            self.assertNotIn("Done!", p_line["prose_text"])

            # 7. Multi-hunk ```patch with <<< REPLACEMENT ... >>> END line-range hunk + ellipsis SEARCH hunk
            multi_patch = (
                "```patch\n"
                "# path: core/client.py\n"
                "# lines: 10-25\n"
                "# expect_start: OLD_HEADER = 1\n"
                "# summary: Update header block\n"
                "<<<<<<< REPLACEMENT\n"
                "NEW_HEADER = 2\n"
                ">>>>>>> END\n"
                "# summary: Simplify big_func via ellipsis\n"
                "<<<<<<< SEARCH\n"
                "def big_func():\n"
                "    ...\n"
                "    return legacy_val\n"
                "=======\n"
                "def big_func():\n"
                "    return new_val\n"
                ">>>>>>> REPLACE\n"
                "```\n"
            )
            p_multi = bridge.parse_playground_markdown(multi_patch)
            self.assertEqual([a["tool"] for a in p_multi["actions"]], ["patch_lines", "patch_literal"])
            self.assertEqual(p_multi["actions"][0]["args"]["start_line"], 10)
            self.assertEqual(p_multi["actions"][0]["args"]["end_line"], 25)
            self.assertEqual(p_multi["actions"][0]["args"]["new_text"], "NEW_HEADER = 2")
            self.assertIn("...", p_multi["actions"][1]["args"]["old_text"])

            # 8. Consecutive ```patch blocks across multiple files + tagged write_file in ONE reply
            multi_block_reply = (
                "Step 1/3 — Patching (api/routes.py): Wire new flag\n"
                "```patch\n"
                "# path: api/routes.py\n"
                "# summary: Pass new flag\n"
                "<<<<<<< SEARCH\n"
                "return run()\n"
                "=======\n"
                "return run(strict=True)\n"
                ">>>>>>> REPLACE\n"
                "```\n\n"
                "Step 2/3 — Patching (web/app.js): Update UI label\n"
                "```patch\n"
                "# path: web/app.js\n"
                "# summary: Update label\n"
                "<<<<<<< SEARCH\n"
                'btn.textContent = "Run";\n'
                "=======\n"
                'btn.textContent = "Run Strict";\n'
                ">>>>>>> REPLACE\n"
                "```\n\n"
                "Step 3/3 — New file (tests/test_flag.py): Add regression test\n"
                "```python\n"
                "# tool: write_file\n"
                "# path: tests/test_flag.py\n"
                "# summary: Add flag regression test\n"
                "def test_flag():\n"
                "    assert True\n"
                "```\n"
                "Wait for output.\n"
            )
            p_batch = bridge.parse_playground_markdown(multi_block_reply)
            self.assertEqual([a["tool"] for a in p_batch["actions"]], ["patch_literal", "patch_literal", "write_file"])
            self.assertEqual([a["args"]["path"] for a in p_batch["actions"]], ["api/routes.py", "web/app.js", "tests/test_flag.py"])
            self.assertNotIn("Wait for output", p_batch["prose_text"])

    def test_every_prompt_guardrail_and_ambiguous_gate_save_vs_inspect(self) -> None:
        import asyncio
        import json as _json
        from models import ProjectCreate, SessionCreate
        from routes import chat as route_chat
        from services import bridge as bridge_svc
        from storage.db import CompanionDB

        if True:
            if True:
                with tempfile.TemporaryDirectory() as tmp:

                    root = Path(tmp) / "proj"
                    root.mkdir()
                    db = CompanionDB(Path(tmp) / "companion.db")
                    proj = db.create_project(
                        ProjectCreate(
                            name="GuardProj",
                            root_path=str(root),
                            python_path=None,
                            directives_path=None,
                            backup_dir="backup",
                            ignore_patterns=[],
                            preset_commands=[],
                        )
                    )
                    sess = db.create_session(
                        SessionCreate(project_id=proj.id, title="S1", api_enabled=False)
                    )

                    # Guardrail appended on follow-up prompt when db is passed
                    b_default = bridge_svc.build_prompt_bundle(proj, "Next step", [], include_directives=False, db=db)
                    self.assertIn("[BRIDGE REMINDER]", b_default["bundle_text"])
                    self.assertIn("Next step", b_default["bundle_text"])
                    self.assertNotIn("Local AI Scout is ON", b_default["bundle_text"])

                    # Follow-up Scout nudge included when scout_enabled=True and omitted when False
                    b_scout_on = bridge_svc.build_prompt_bundle(
                        proj, "Next step", [], include_directives=False, scout_enabled=True, db=db
                    )
                    self.assertIn("Local AI Scout is ON", b_scout_on["bundle_text"])
                    b_scout_off = bridge_svc.build_prompt_bundle(
                        proj, "Next step", [], include_directives=False, scout_enabled=False, db=db
                    )
                    self.assertNotIn("Local AI Scout is ON", b_scout_off["bundle_text"])

                    # Empty guardrail in DB disables appending it
                    db.set_setting("prompt_bridge_guardrail", "")
                    b_disabled = bridge_svc.build_prompt_bundle(proj, "Next step", [], include_directives=False, db=db)
                    self.assertEqual(b_disabled["bundle_text"], "Next step")

                    # Ambiguous Python gate -> resolved via save_as_file with custom target_path
                    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=None)))
                    g_save = db.create_gate(
                        sess.id,
                        "propose_inspection_script",
                        {
                            "path": "inspect_tmp.py",
                            "content": "def helper():\n    return 7\n",
                            "summary": "Helper module",
                            "intent_ambiguous": True,
                        },
                        {"category": "script", "intent_ambiguous": True, "script_path": "inspect_tmp.py"},
                    )
                    res_save = asyncio.run(
                        route_chat.resolve_tool_gate(
                            g_save["id"],
                            SimpleNamespace(action="save_as_file", target_path="pkg/helper.py", reason=None),
                            req,
                        )
                    )
                    self.assertTrue((root / "pkg" / "helper.py").is_file())
                    self.assertFalse((root / "inspect_tmp.py").exists())
                    last_ev = _json.loads(res_save["messages"][-1].text)
                    self.assertEqual(last_ev["tool_name"], "write_file")
                    self.assertEqual(last_ev["response"]["path"], "pkg/helper.py")

                    # Ambiguous Python gate -> resolved via write_run_and_delete with custom target_path
                    g_run = db.create_gate(
                        sess.id,
                        "propose_inspection_script",
                        {
                            "path": "inspect_tmp.py",
                            "content": "print('RUN_OK')\n",
                            "summary": "Quick check",
                            "intent_ambiguous": True,
                        },
                        {"category": "script", "intent_ambiguous": True, "script_path": "inspect_tmp.py"},
                    )
                    res_run = asyncio.run(
                        route_chat.resolve_tool_gate(
                            g_run["id"],
                            SimpleNamespace(action="write_run_and_delete", target_path="tmp_check.py", reason=None),
                            req,
                        )
                    )
                    self.assertFalse((root / "tmp_check.py").exists())
                    run_ev = _json.loads(res_run["messages"][-1].text)
                    self.assertTrue(run_ev["response"]["executed"])
                    self.assertTrue(run_ev["response"]["deleted"])
                    self.assertIn("RUN_OK", run_ev["response"]["output_preview"])
        if False:
            pass

    def test_inspection_script_auto_approve_patch_and_rerun_loop(self) -> None:
        import asyncio
        import json as _json
        from models import BridgeParseRequest, ProjectCreate, SessionCreate
        from routes import bridge as route_bridge
        from routes import chat as route_chat
        from services import bridge as bridge_svc
        from storage.db import CompanionDB

        if True:
            if True:
                with tempfile.TemporaryDirectory() as tmp:

                    root = Path(tmp) / "proj"
                    root.mkdir()
                    app_file = root / "app.py"
                    app_file.write_text("VALUE = 1\n", encoding="utf-8")

                    db = CompanionDB(Path(tmp) / "companion.db")
                    proj = db.create_project(
                        ProjectCreate(
                            name="InspLoopProj",
                            root_path=str(root),
                            python_path=sys.executable,
                            directives_path=None,
                            backup_dir="backup",
                            ignore_patterns=[],
                            preset_commands=[],
                        )
                    )
                    sess = db.create_session(
                        SessionCreate(project_id=proj.id, title="InspLoop", api_enabled=False)
                    )
                    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, gemini=None)))

                    # 1. Stage failing inspection script & approve once via write_run_and_delete
                    g1 = db.create_gate(
                        sess.id,
                        "propose_inspection_script",
                        {
                            "path": "inspect_probe.py",
                            "content": "import sys\nsys.stderr.write('TypeError: boom\\n')\nsys.exit(1)\n",
                            "summary": "Probe country converter",
                        },
                        {"summary": "Probe country converter", "delete_on_success": True},
                    )
                    db.add_message(sess.id, role="model", text="Running initial probe")
                    res1 = asyncio.run(
                        route_chat.resolve_tool_gate(
                            g1["id"], SimpleNamespace(action="write_run_and_delete", reason=None), req
                        )
                    )
                    self.assertTrue((root / "inspect_probe.py").is_file())
                    self.assertIn("TypeError: boom", res1["bridge_reply_text"])

                    # 2. Paste ONLY a raw ```patch fixing inspect_probe.py -> auto-approves, auto-reruns, passes & deletes
                    patch_md = (
                        "Step 1/1 — Patching (inspect_probe.py): Print result cleanly\n"
                        "```patch\n"
                        "# path: inspect_probe.py\n"
                        "<<<<<<< SEARCH\n"
                        "import sys\n"
                        "sys.stderr.write('TypeError: boom\\n')\n"
                        "sys.exit(1)\n"
                        "=======\n"
                        "print('PROBE_FIXED_77')\n"
                        ">>>>>>> REPLACE\n"
                        "```\n"
                    )
                    res2 = asyncio.run(
                        route_bridge.parse_bridge_response(
                            BridgeParseRequest(session_id=sess.id, raw_markdown=patch_md, auto_approve_reads=True),
                            req,
                        )
                    )
                    self.assertEqual(res2["pending_gates"], [])
                    self.assertGreaterEqual(res2["auto_executed_count"], 1)
                    self.assertFalse((root / "inspect_probe.py").exists())
                    self.assertIn("auto-approved inspection script patch", res2["bridge_reply_text"])
                    self.assertIn("PROBE_FIXED_77", res2["bridge_reply_text"])

                    # 3. Safety boundary: non-inspection file (app.py) MUST still create a pending gate,
                    #    and deleted inspect_probe.py is no longer auto-approved.
                    safety_md = (
                        "```patch\n"
                        "# path: app.py\n"
                        "<<<<<<< SEARCH\n"
                        "VALUE = 1\n"
                        "=======\n"
                        "VALUE = 2\n"
                        ">>>>>>> REPLACE\n"
                        "```\n"
                    )
                    res3 = asyncio.run(
                        route_bridge.parse_bridge_response(
                            BridgeParseRequest(session_id=sess.id, raw_markdown=safety_md, auto_approve_reads=True),
                            req,
                        )
                    )
                    self.assertEqual(len(res3["pending_gates"]), 1)
                    self.assertEqual(res3["pending_gates"][0]["tool_name"], "patch_literal")
                    self.assertEqual(app_file.read_text(encoding="utf-8"), "VALUE = 1\n")

                    # Clear pending gate on app.py cleanly so next batch starts fresh
                    asyncio.run(
                        route_chat.resolve_tool_gate(
                            res3["pending_gates"][0]["id"],
                            SimpleNamespace(action="deny", reason="keep untouched"),
                            req,
                        )
                    )

                    # 4. Duplicate-run prevention: patch_literal + run_and_delete_script in one turn runs ONCE
                    sess2 = db.create_session(
                        SessionCreate(project_id=proj.id, title="NoDoubleRun", api_enabled=False)
                    )
                    g2 = db.create_gate(
                        sess2.id,
                        "propose_inspection_script",
                        {
                            "path": "inspect_once.py",
                            "content": "import sys\nsys.exit(1)\n",
                            "summary": "Check once",
                        },
                        {"summary": "Check once", "delete_on_success": True},
                    )
                    asyncio.run(
                        route_chat.resolve_tool_gate(
                            g2["id"], SimpleNamespace(action="write_run_and_delete", reason=None), req
                        )
                    )
                    both_json = (
                        "Patching and re-running inspect_once.py:\n"
                        "```json\n"
                        "[\n"
                        '  {"tool": "patch_literal", "path": "inspect_once.py", "old_text": "import sys\\nsys.exit(1)", "new_text": "print(\'RAN_EXACTLY_ONCE\')"},\n'
                        '  {"tool": "run_and_delete_script", "path": "inspect_once.py"}\n'
                        "]\n"
                        "```\n"
                    )
                    res4 = asyncio.run(
                        route_bridge.parse_bridge_response(
                            BridgeParseRequest(session_id=sess2.id, raw_markdown=both_json, auto_approve_reads=True),
                            req,
                        )
                    )
                    self.assertEqual(res4["pending_gates"], [])
                    self.assertFalse((root / "inspect_once.py").exists())
                    self.assertEqual(res4["bridge_reply_text"].count("RAN_EXACTLY_ONCE"), 1)

                    # 5. Stopped-by-user bridge formatting check
                    stopped_md = bridge_svc.format_bridge_tool_results(
                        [
                            {
                                "tool_name": "run_script",
                                "response": {
                                    "command": "python hang.py",
                                    "exec_id": "exec-stop",
                                    "exit_code": -1,
                                    "duration_ms": 2100,
                                    "passed": False,
                                    "cancelled": True,
                                    "output_bytes": 0,
                                    "output_preview": "",
                                },
                            }
                        ]
                    )
                    self.assertIn("STOPPED BY USER", stopped_md)


if __name__ == "__main__":
    unittest.main()
