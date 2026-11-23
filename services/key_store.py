"""
services/key_store.py - Local JSON persistence and verification for the Gemini API key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Tuple

from config import GEMINI_KEY_PATH


def has_configured_api_key(gemini: Any) -> bool:
    """Returns True if a GeminiGateway has a client factory (tests) or a saved/env API key."""
    if gemini is None:
        return False
    if getattr(gemini, "_client_factory", None) is not None:
        return True
    try:
        api_key, _ = load_api_key(gemini.key_path)
        return bool(api_key)
    except RuntimeError:
        return False


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
    """Performs a lightweight models.list check via google-genai to verify key validity."""
    clean = api_key.strip()
    if not clean:
        return False, "API key is empty."

    from google import genai

    try:
        client = genai.Client(api_key=clean)
        pager = client.models.list(config={"page_size": 5})
        next(iter(pager), None)
        return True, ""
    except Exception as exc:
        return False, str(exc)
