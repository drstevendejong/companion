"""
config.py - Companion runtime constants, model catalog, and ignore defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import FrozenSet, List, Tuple

APP_ROOT: Path = Path(__file__).resolve().parent
PROMPTS_DIR: Path = APP_ROOT / "static" / "prompts"
DATA_DIR: Path = Path(os.environ.get("COMPANION_DATA_DIR", str(APP_ROOT / "data")))
DB_PATH: Path = DATA_DIR / "companion.db"
GEMINI_KEY_PATH: Path = DATA_DIR / "gemini_key.json"

DEFAULT_HOST: str = os.environ.get("COMPANION_HOST", "127.0.0.1")
DEFAULT_PORT: int = int(os.environ.get("COMPANION_PORT", "8765"))

DEFAULT_MODEL: str = "gemini-4-hydrogen"
DEFAULT_COMMIT_MODEL: str = "gemini-4-hydrogen"
DEFAULT_THINKING_LEVEL: str = "HIGH"

AVAILABLE_MODELS: List[Tuple[str, str, int]] = [
    ("gemini-4-hydrogen", "Gemini 4 Hydrogen (Fast & Balanced)", 1_048_576),
    ("gemini-4-argon", "Gemini 4 Argon (Coding & Complex Reasoning)", 1_048_576),
    ("gemini-4-carbon", "Gemini 4 Carbon (Deep Architecture & Debug)", 1_048_576),
    ("gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite (Minimal Latency)", 1_048_576),
]

THINKING_LEVELS: List[str] = ["LOW", "MEDIUM", "HIGH", "XHIGH"]

DEFAULT_IGNORE_PATTERNS: FrozenSet[str] = frozenset({
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "backup",
    "data",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".DS_Store",
    "Thumbs.db",
    "dist",
    "build",
    "*.pyc",
    "*.pyo",
    "*.egg-info",
})

MAX_READ_CHARS_SOFT: int = 120_000
MAX_COMMAND_OUTPUT_CHARS: int = 150_000
COMMAND_TIMEOUT_SECONDS: int = 180

BACKUP_MAX_SNAPSHOTS: int = 10
BACKUP_MAX_AGE_DAYS: int = 3
BACKUP_MAX_BYTES: int = 5 * 1024 * 1024
BACKUP_MIN_KEEP: int = 2

CHECKPOINT_MAX_KEEP: int = 0
CHECKPOINT_MAX_AGE_DAYS: int = 3
CHECKPOINT_MAX_BYTES: int = 10 * 1024 * 1024
CHECKPOINT_MAX_FILE_BYTES: int = 5 * 1024 * 1024
CHECKPOINT_MAX_SNAPSHOT_BYTES: int = 25 * 1024 * 1024

DB_MAX_MESSAGES_PER_SESSION: int = 60
DB_KEEP_FULL_TOOL_EVENTS_TAIL: int = 6
DB_MAX_RESOLVED_GATES: int = 20
DB_MAX_PATCH_RECORDS_PER_PROJECT: int = 40
DB_MAX_RESOLVED_CHECKPOINTS_PER_PROJECT: int = 15
DB_VACUUM_MIN_BYTES: int = 1 * 1024 * 1024

SCOUT_DEFAULT_MAX_STEPS: int = 8
SCOUT_DEFAULT_MAX_TOKENS: int = 30_000
SCOUT_MAX_TOOL_READ_CHARS: int = 24_000
SCOUT_COOLDOWN_SECONDS: int = 60
OUTPUT_SUMMARIZE_THRESHOLD_CHARS: int = 4_000
OUTPUT_FAILURE_TRIAGE_MIN_CHARS: int = 1_500
OUTPUT_BUFFER_MAX_ENTRIES: int = 20

