# Gemini Vibe Companion

A modular, project-agnostic local companion for Gemini-assisted coding alongside your IDE. Supports both **Playground Bridge mode** (0 API tokens via copy/paste with AI Studio Playground) and **Gemini API mode**.

## Features
- **Two Modes — Playground Bridge (Default) & Gemini API:**
  - **Playground Bridge (0 API Tokens):** Two clear actions—**Copy Starter / Prompt → Playground** (`Ctrl+Enter`) and **Paste & Run AI Reply** (with automatic fallback paste box if clipboard access is blocked). Also auto-detects if an AI JSON/script reply was accidentally pasted into the prompt box and offers **Run & Stage Tools** in 1 click. Formats local results, script stdout, or newly generated `.md` bundles back into your clipboard.
  - **Gemini API Mode:** Optional direct API integration with local key persistence (`data/gemini_key.json` or `GEMINI_API_KEY`), live free/paid model catalog probing, and real-time RPM/TPM/RPD rate-limit gauges.
- **Playground Bridge Hard-Stop:** Enforces an immediate stop after the closing ` ``` ` of the first JSON command block, automatically truncating any runaway model text or hallucinated `## Local Workspace Tool Results`.
- **Human-Gated Tool Loop:** Every workspace probe (`list_tree`, `extract_headers`, regex/literal `search_code` scoped by `subpath`/`path_glob`/`start_line`/`end_line`, `read_lines`, `read_file`, `read_command_output`), file edit (`patch_literal`, `patch_lines`, `write_file`, `delete_file`, `rename_file`), script (`propose_script`, `run_script`, `run_and_delete_script`), `run_tests`, and optional Local AI Scout (`scout_workspace`, `update_header`) request pauses at a sticky approval bar with estimated tokens or unified diff + Python AST validation.
- **Local AI Scout (`-lite` Models at `MEDIUM` Thinking, Bridge + API Modes):** Optional topbar **Scout: On/Off** & **Auto: On/Off** toggles (persisted in SQLite; requires a configured Gemini API key). Strictly selects the lightest usable `-lite` model from the catalog with automatic model hopping on `429`/transient errors and fresh ephemeral context per run. Announces ON/OFF state changes explicitly into Playground Bridge prompts/replies. Supports gated `scout_workspace` multi-step read investigations with live WebSocket step balloons + **Pause/Resume** & **Stop (Keep Findings)** controls, `focus`-aware command/test output summarization (>4,000 chars) & failure triage (backed by rolling `exec_id` inspection via `read_command_output`), and opt-in `update_header` diff drafting.
- **0-Token `extract_headers` Structural Index:** Works even when Scout is OFF—deterministically extracts top-of-file docstrings/comments and top-level classes/functions across matching files.
- **3-Tier Patching, File Ops & Smart Failure Diagnostics:** Surgical `patch_literal`, line-range `patch_lines`, full `write_file`, `delete_file`, `rename_file`, `propose_script`, `run_script`, and `run_and_delete_script`—all backed up before modifying disk. `propose_script`, `run_script`, and `run_and_delete_script` capture stdout/stderr even on exit code `0` for direct handoff back to AI Studio; `run_and_delete_script` (and **Write, Run & Delete on Pass** on `propose_script`) automatically removes temporary inspection scripts once they exit cleanly while keeping failing scripts on disk for patching. When `patch_literal` misses a block, Companion computes closest line numbers and a compact diff hint for fast model self-correction.
- **Atomic Project Checkpoints & Console Savepoint Control:** Before executing the first write in a batch, Companion initializes a lean Copy-on-Write project savepoint inside `<project_root>/<backup_dir>/.checkpoints/` (backing up only files actually modified or deleted) and displays a green **Savepoint** banner with 1-click **Restore**. Passing a full gated `run_tests` (or running the configured test command in **Console**) auto-commits the checkpoint; after any manual verification run in **Console**, click **Commit Savepoint** right inside the Console tab (or **Commit Anyway** in chat). Both per-file `backup/` snapshots and `.checkpoints/` self-clean automatically by age, count, and byte budget—and SQLite (`data/companion.db`) auto-prunes resolved gates, older payloads, and excess messages (>60 per session) and runs `VACUUM` + WAL checkpointing automatically.
- **Live-Streaming Console & Gated `run_tests` (Full + Quick/Targeted):** Command output from **Console**, `run_tests`, `propose_script`, `run_script`, and `run_and_delete_script` streams live over WebSocket into the Console drawer tab and running chat cards. Optional `fast: true` or `target` runs your configured Quick Check Command or a sub-second single-file syntax check (`.py`, `.js`, `.json`, `.html`, `.css`) while keeping the savepoint active until a full `run_tests` passes. Failed runs automatically extract high-signal error/traceback excerpts for copying (with a separate **Copy Full Output** option).
- **UI-Driven Git Workflow (Right Drawer → Git Tab):** Calls `git` locally via Python `subprocess` (`git status`, `git diff`, `git add -A`, `git commit`, `git push`) triggered exclusively by user clicks (**Refresh**, **Draft with Gemini** / **Draft with Scout** / **Copy Git Diff**, **Update Headers** when Scout is active, **Commit**, **Commit & Push**). Git is intentionally NOT exposed as an autonomous AI tool.
- **Externalized Prompts (`static/prompts/`):** Customize Companion's prompts cleanly on disk without editing code (changes take effect live on the next turn or bundle copy):
  - `static/prompts/system_prompt.txt` — Core agent discipline & tool guidance
  - `static/prompts/bridge_tool_contract.txt` — Playground Bridge JSON tool contract
  - `static/prompts/git_commit_prompt.txt` — Commit message drafting instructions (`{diff}` template)
- **Zero-Token Test Suite:** Full unit and workflow tests under `tests/` mocking the Gemini API boundary.

## Quick Start
1. Extract this folder anywhere on your machine.
2. Launch (auto-installs dependencies from `pyproject.toml` and opens Chrome automatically at `http://127.0.0.1:8765`; pass `--no-browser` to disable):
   ```bash
   python main.py
   ```
3. Add a project profile (project root, optional Directives `.md`, `backup/` dir, Post-Patch Test Command) and start working in Playground Bridge mode (or configure a Gemini API key in Settings to use API mode).

## Running the Zero-Token Tests
```bash
python -m unittest discover -s tests -v
```
(Or `pytest -v` if pytest is installed.)
