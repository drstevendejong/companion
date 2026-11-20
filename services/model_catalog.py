"""
services/model_catalog.py - Runtime Gemini model discovery and Free/Paid tier probing.

Queries `client.models.list()` at the time of use so renamed, newly launched, and legacy
models appear automatically without hardcoding model lists, and probes/caches which models
work on a Free Tier key vs requiring a paid API key.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import AVAILABLE_MODELS, THINKING_LEVELS
from services.quota_parser import analyze_gemini_error

CACHE_TTL_SECONDS = 7 * 24 * 3600
FREE_KEY_NOTICE = "Provide a paid API key to access more models"

EXCLUDED_MODEL_SUBSTRINGS = (
    "-tts",
    "-image",
    "-live",
    "-transcribe",
    "-translate",
    "-embedding",
    "nano-banana",
    "omni",
    "robotics",
    "computer-use",
    "deep-research",
    "-audio",
    "aqa",
    "bisect",
    "learnlm",
)


def key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def load_cache(cache_path: Path) -> Dict[str, Any]:
    if not cache_path.is_file():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache_path: Path, cache: Dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def is_interactive_gemini_chat_model(model_obj: Any) -> bool:
    """Returns True if the live Model metadata represents an interactive Gemini text/code model."""
    raw_name = str(getattr(model_obj, "name", "") or "")
    model_id = raw_name.removeprefix("models/").lower()
    if not model_id.startswith("gemini-"):
        return False

    actions = getattr(model_obj, "supported_actions", None)
    if actions is not None and "generateContent" not in actions:
        return False

    for bad in EXCLUDED_MODEL_SUBSTRINGS:
        if bad in model_id:
            return False

    return True


def _model_sort_key(model: Dict[str, Any]) -> Tuple[Any, ...]:
    """Sorts newest Gemini generations cleanly first while preserving stable readability."""
    mid = model["id"].lower()
    ver_match = re.search(r"gemini-(\d+)(?:\.(\d+))?", mid)
    major = int(ver_match.group(1)) if ver_match else 0
    minor = int(ver_match.group(2) or 0) if ver_match else 0
    # Prefer non-lite balanced/pro variants ahead of lite within same generation
    is_lite = 1 if "lite" in mid else 0
    is_snapshot = 1 if re.search(r"-\d{3,4}$|-exp", mid) else 0
    return (-major, -minor, is_lite, is_snapshot, mid)


def _deduplicate_models(raw_models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in raw_models:
        by_id[item["id"]] = item

    # Hide redundant pinned numeric snapshots like gemini-2.5-flash-001 when gemini-2.5-flash exists
    filtered: List[Dict[str, Any]] = []
    for mid, item in by_id.items():
        base_match = re.match(r"^(gemini-[^/]+)-\d{3}$", mid)
        if base_match and base_match.group(1) in by_id:
            continue
        filtered.append(item)

    filtered.sort(key=_model_sort_key)
    return filtered


def _probe_single_model(client: Any, model_id: str) -> Tuple[str, Optional[str]]:
    """
    Runs a 1-token minimal probe to classify model access on the active API key.
    Returns (status, detected_key_tier) where:
      status in {'usable', 'paid_only', 'unavailable'}
      detected_key_tier in {'free', None}
    """
    from google.genai import types

    config_kwargs: Dict[str, Any] = {"max_output_tokens": 1}
    if hasattr(types, "AutomaticFunctionCallingConfig"):
        config_kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
    if not model_id.startswith(("gemini-1.", "gemini-2.")) and hasattr(types, "ThinkingConfig") and hasattr(types, "ThinkingLevel"):
        low_lvl = getattr(types.ThinkingLevel, "LOW", None)
        if low_lvl is not None:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=low_lvl)

    try:
        client.models.generate_content(
            model=model_id,
            contents=".",
            config=types.GenerateContentConfig(**config_kwargs),
        )
        return "usable", None
    except Exception as exc:
        info = analyze_gemini_error(exc)
        if info["is_auth"]:
            raise
        if info["is_paid_required"]:
            return "paid_only", "free"
        if info["is_429"]:
            return "usable", "free" if info["is_free_tier"] else None
        if info["is_not_found"] or "not supported for generatecontent" in info["raw"].lower():
            return "unavailable", None
        # Any other unexpected error during probe shouldn't hide a potentially usable model
        return "usable", None


def record_model_runtime_error(
    cache_path: Path,
    api_key: str,
    model_id: str,
    error_text: Any,
) -> Optional[str]:
    """Updates cached tier knowledge when a live request encounters a 429 quota error."""
    if not api_key:
        return None
    info = analyze_gemini_error(error_text)
    if not info["is_429"] and not info["is_not_found"]:
        return None

    fp = key_fingerprint(api_key)
    cache = load_cache(cache_path)
    entry = cache.get(fp) or {"updated_at": time.time(), "key_tier": "unknown", "models": {}}
    models_map = entry.setdefault("models", {})
    existing = models_map.get(model_id) or {"id": model_id, "label": model_id, "context_limit": 1_048_576}

    if info["is_paid_required"]:
        existing["tier_status"] = "paid_only"
        entry["key_tier"] = "free"
    elif info["is_free_tier"]:
        existing["tier_status"] = existing.get("tier_status") or "usable"
        entry["key_tier"] = "free"
    elif info["is_not_found"]:
        existing["tier_status"] = "unavailable"

    models_map[model_id] = existing
    entry["updated_at"] = time.time()
    cache[fp] = entry
    save_cache(cache_path, cache)
    return existing.get("tier_status")


def get_fallback_catalog(notice: Optional[str] = None) -> Dict[str, Any]:
    return {
        "models": [
            {"id": m_id, "label": label, "context_limit": ctx}
            for m_id, label, ctx in AVAILABLE_MODELS
        ],
        "thinking_levels": THINKING_LEVELS,
        "key_tier": "unknown",
        "paid_only_count": 0,
        "paid_only_models": [],
        "probing_needed": False,
        "notice": notice,
        "source": "fallback",
    }


def fetch_dynamic_model_catalog(
    client: Any,
    api_key: str,
    cache_path: Path,
    force_refresh: bool = False,
    probe: bool = True,
) -> Dict[str, Any]:
    """
    Discovers live Gemini chat models via `client.models.list()`.
    When `probe=False`, returns immediately with live + cached entries and sets
    `probing_needed=True` if any uncached models should be background-probed for tier access.
    """
    if not api_key or client is None or not hasattr(client.models, "list"):
        return get_fallback_catalog()

    fp = key_fingerprint(api_key)
    cache = load_cache(cache_path)
    now = time.time()
    cached_entry = cache.get(fp)

    cache_fresh = (
        not force_refresh
        and isinstance(cached_entry, dict)
        and (now - float(cached_entry.get("updated_at", 0))) < CACHE_TTL_SECONDS
        and isinstance(cached_entry.get("models"), dict)
        and len(cached_entry["models"]) > 0
    )

    if cache_fresh:
        cached_models = list(cached_entry["models"].values())
        unprobed = any(
            m.get("tier_status") not in ("usable", "paid_only", "unavailable")
            for m in cached_models
        )
        if not unprobed or not probe:
            return _format_catalog_response(
                cached_models,
                key_tier=str(cached_entry.get("key_tier", "unknown")),
                source="cache",
                probing_needed=unprobed,
            )

    # Query live model list from Google at the time of use (fast single GET)
    live_raw: List[Dict[str, Any]] = []
    for m in client.models.list():
        if not is_interactive_gemini_chat_model(m):
            continue
        mid = str(getattr(m, "name", "")).removeprefix("models/")
        display = str(getattr(m, "display_name", "") or mid).strip()
        ctx = int(getattr(m, "input_token_limit", None) or 1_048_576)
        live_raw.append(
            {
                "id": mid,
                "label": f"{display} ({mid})" if display.lower() != mid.lower() and mid not in display.lower() else display,
                "context_limit": ctx,
                "tier_status": "unprobed",
            }
        )

    if not live_raw:
        return get_fallback_catalog()

    deduped = _deduplicate_models(live_raw)

    prev_models = (cached_entry or {}).get("models", {}) if not force_refresh else {}
    detected_tier = str((cached_entry or {}).get("key_tier", "unknown")) if not force_refresh else "unknown"
    to_probe: List[Dict[str, Any]] = []
    for item in deduped:
        prev = prev_models.get(item["id"])
        if prev and prev.get("tier_status") in ("usable", "paid_only", "unavailable"):
            item["tier_status"] = prev["tier_status"]
        else:
            to_probe.append(item)

    if not probe and to_probe:
        stored_map = {item["id"]: item for item in deduped}
        cache[fp] = {
            "updated_at": now,
            "key_tier": detected_tier,
            "models": stored_map,
        }
        save_cache(cache_path, cache)
        return _format_catalog_response(
            deduped,
            key_tier=detected_tier,
            source="live",
            probing_needed=True,
        )

    if to_probe and probe:
        with ThreadPoolExecutor(max_workers=min(8, len(to_probe))) as pool:
            futures = {
                pool.submit(_probe_single_model, client, item["id"]): item
                for item in to_probe
            }
            for fut in as_completed(futures):
                item = futures[fut]
                status, tier_hint = fut.result()
                item["tier_status"] = status
                if tier_hint == "free":
                    detected_tier = "free"

        if detected_tier == "unknown" and all(m["tier_status"] == "usable" for m in deduped):
            detected_tier = "paid"

    stored_map = {item["id"]: item for item in deduped}
    cache[fp] = {
        "updated_at": now,
        "key_tier": detected_tier,
        "models": stored_map,
    }
    save_cache(cache_path, cache)

    return _format_catalog_response(
        deduped,
        key_tier=detected_tier,
        source="live",
        probing_needed=False,
    )


def _format_catalog_response(
    models: List[Dict[str, Any]],
    key_tier: str,
    source: str,
    probing_needed: bool = False,
) -> Dict[str, Any]:
    ordered = sorted(models, key=_model_sort_key)
    selectable: List[Dict[str, Any]] = []
    paid_only: List[str] = []

    for item in ordered:
        status = item.get("tier_status", "unprobed")
        if status == "unavailable":
            continue
        if status == "paid_only" or (key_tier == "free" and status == "paid_only"):
            paid_only.append(item["id"])
            continue
        selectable.append(
            {
                "id": item["id"],
                "label": item.get("label", item["id"]),
                "context_limit": int(item.get("context_limit", 1_048_576)),
            }
        )

    notice = FREE_KEY_NOTICE if (key_tier == "free" or len(paid_only) > 0) else None
    return {
        "models": selectable,
        "thinking_levels": THINKING_LEVELS,
        "key_tier": key_tier,
        "paid_only_count": len(paid_only),
        "paid_only_models": paid_only,
        "probing_needed": probing_needed,
        "notice": notice,
        "source": source,
    }
