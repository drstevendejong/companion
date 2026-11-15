"""
services/key_store.py - Local JSON persistence and verification for the Gemini API key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Tuple

from config import GEMINI_KEY_PATH


def load_api_key(key_path: Path = GEMINI_KEY_PATH) -> Tuple[str, str]:
    """
    Loads the Gemini API key from local JSON storage first, then GEMINI_API_KEY env var.
    Returns (api_key, source) where source is 'file', 'env', or 'none'.
    """
    path = Path(key_path)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Corrupt key config at {path}: {exc}") from exc
        raw = str(data.get("api_key", "")).strip()
        if raw:
            return raw, "file"

    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key, "env"
    return "", "none"


def save_api_key(api_key: str, key_path: Path = GEMINI_KEY_PATH) -> Path:
    """Persists the Gemini API key to local JSON storage."""
    clean = api_key.strip()
    if not clean:
        raise ValueError("API key cannot be empty.")

    path = Path(key_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"api_key": clean}, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def clear_api_key(key_path: Path = GEMINI_KEY_PATH) -> bool:
    """Removes the stored local API key file if present."""
    path = Path(key_path)
    if path.is_file():
        path.unlink()
        return True
    return False


def mask_api_key(api_key: str) -> str:
    """Returns a non-sensitive masked preview of the key for UI display."""
    clean = api_key.strip()
    if not clean:
        return ""
    if len(clean) <= 8:
        return "••••"
    return f"{clean[:4]}••••{clean[-4:]}"


def validate_api_key_with_gemini(api_key: str) -> Tuple[bool, str]:
    """Performs a lightweight models.get check via google-genai to verify key validity."""
    clean = api_key.strip()
    if not clean:
        return False, "API key is empty."

    from google import genai

    try:
        client = genai.Client(api_key=clean)
        client.models.get(model="gemini-4-hydrogen")
        return True, ""
    except Exception as exc:
        return False, str(exc)
