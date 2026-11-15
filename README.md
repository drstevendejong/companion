# Gemini Vibe Companion

A modular, project-agnostic local companion for Gemini-assisted coding alongside PyCharm and Chrome Debug Mode (`:9222`).

## Features
- **Local API Key Modal:** Automatically prompts on startup if no valid key is saved; persists locally to `data/gemini_key.json` (with optional `GEMINI_API_KEY` env var fallback).
- **Human-Gated Tool Loop:** Gemini probes (`list_tree`, `search_code`, `read_lines`, `read_file`) and shows estimated tokens before you click **Approve** or **Edit Range**.
- **3-Tier Patching + Project Backups:** Surgical literal replacement, line-range replacement, full file write, or custom script execution—with unified diffs, Python AST pre-flight checks, and automatic snapshots stored inside `<project_root>/backup/` with 1-click Undo.
- **Post-Patch Test Gate & Paste into Prompt:** Run your project's validator/test suite after patches; outputs stay local with a 1-click **Paste into Prompt** button (never auto-flooding Gemini).
- **Chrome CDP (`:9222`):** Tab selector, 1-click viewport/full-page screenshot, and recent console/exception capture.
- **Git Workflow:** Live `git status` & diff, Gemini-drafted commit messages, and Commit / Commit & Push.
- **Zero-Token Test Suite:** Full unit and workflow tests under `tests/` mocking the Gemini API boundary.

## Quick Start
1. Extract this folder anywhere on your machine.
2. Launch (auto-installs dependencies from `pyproject.toml` and opens Chrome automatically at `http://127.0.0.1:8765`; pass `--no-browser` to disable):
   ```bash
   python main.py
   ```
3. When prompted on startup, paste your Gemini API key into the modal (saved locally to `data/gemini_key.json`), then add your project profile and start chatting.

## Running the Zero-Token Tests
```bash
python -m unittest discover -s tests -v
```
(Or `pytest -v` if pytest is installed.)
