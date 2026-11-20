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
DEFAULT_CDP_PORT: int = int(os.environ.get("COMPANION_CDP_PORT", "9222"))

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

BACKUP_MAX_SNAPSHOTS: int = 20
BACKUP_MAX_AGE_DAYS: int = 7
BACKUP_MAX_BYTES: int = 20 * 1024 * 1024
BACKUP_MIN_KEEP: int = 3

CHECKPOINT_MAX_KEEP: int = 5
CHECKPOINT_MAX_AGE_DAYS: int = 7
CHECKPOINT_MAX_BYTES: int = 80 * 1024 * 1024
CHECKPOINT_MAX_FILE_BYTES: int = 10 * 1024 * 1024
CHECKPOINT_MAX_SNAPSHOT_BYTES: int = 100 * 1024 * 1024
