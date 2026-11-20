"""
services/quota_parser.py - Parses Gemini API errors (429 RESOURCE_EXHAUSTED, auth, etc.)
into concise actionable guidance (WAIT A MINUTE / WAIT A DAY / PAID API KEY REQUIRED).
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional


def analyze_gemini_error(raw_error: Any) -> Dict[str, Any]:
    """Extracts structured quota, tier, and retry info from any Gemini exception string."""
    text = str(raw_error or "").strip()
    lower = text.lower()

    is_429 = (
        "429" in text
        or "resource_exhausted" in lower
        or "exceeded your current quota" in lower
    )
    is_auth = (
        "api_key_invalid" in lower
        or "api key not valid" in lower
        or "permission_denied" in lower
        or ("403" in text and "permission" in lower)
    )
    is_not_found = "404" in text and "not_found" in lower

    is_free_tier = "freetier" in lower or "free_tier" in lower

    quota_match = re.search(r"['\"]?quotaValue['\"]?\s*:\s*['\"](\d+)['\"]", text)
    if not quota_match:
        quota_match = re.search(r"\blimit\s*:\s*(\d+)\b", text)
    quota_value: Optional[int] = int(quota_match.group(1)) if quota_match else None

    quota_id_match = re.search(r"['\"]?quotaId['\"]?\s*:\s*['\"]([^'\"]+)['\"]", text)
    quota_id = quota_id_match.group(1) if quota_id_match else ""

    metric_match = re.search(r"Quota exceeded for metric:\s*([^\s,]+)", text)
    if not metric_match:
        metric_match = re.search(r"['\"]?quotaMetric['\"]?\s*:\s*['\"]([^'\"]+)['\"]", text)
    quota_metric = metric_match.group(1) if metric_match else ""

    model_match = re.search(r"['\"]?model['\"]?\s*:\s*['\"]([a-zA-Z0-9._-]+)['\"]", text)
    if not model_match:
        model_match = re.search(r"\bmodel:\s*([a-zA-Z0-9._-]+)", text)
    model_name = model_match.group(1) if model_match else ""

    retry_seconds: Optional[int] = None
    retry_match = re.search(r"retry in\s*([0-9]+(?:\.[0-9]+)?)s", lower)
    if not retry_match:
        retry_match = re.search(r"['\"]?retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s['\"]?", text)
    if retry_match:
        retry_seconds = max(1, math.ceil(float(retry_match.group(1))))

    is_paid_required = bool(is_429 and quota_value == 0)

    window = "unknown"
    if is_429:
        combined = f"{quota_id} {quota_metric} {text}".lower()
        if "perday" in combined or "per_day" in combined or "per day" in combined:
            window = "day"
        elif "perminute" in combined or "per_minute" in combined or "per minute" in combined or retry_seconds is not None:
            window = "minute"

    headline = "Gemini API Error"
    if is_paid_required:
        headline = "PAID API KEY REQUIRED"
    elif is_429:
        if window == "day":
            headline = "WAIT A DAY"
        elif window == "minute":
            headline = f"WAIT A MINUTE (~{retry_seconds}s)" if retry_seconds else "WAIT A MINUTE"
        else:
            headline = f"RATE LIMITED (~{retry_seconds}s)" if retry_seconds else "RATE LIMITED"
    elif is_auth:
        headline = "INVALID API KEY"
    elif is_not_found:
        headline = "MODEL NOT AVAILABLE"

    return {
        "is_429": is_429,
        "is_auth": is_auth,
        "is_not_found": is_not_found,
        "is_free_tier": is_free_tier,
        "is_paid_required": is_paid_required,
        "quota_value": quota_value,
        "quota_id": quota_id,
        "quota_metric": quota_metric,
        "model": model_name,
        "retry_seconds": retry_seconds,
        "window": window,
        "headline": headline,
        "raw": text,
    }


def format_gemini_error(raw_error: Any) -> str:
    """Formats a Gemini exception into a concise, human-readable diagnostic banner."""
    info = analyze_gemini_error(raw_error)
    text = info["raw"]

    if info["is_paid_required"]:
        model_part = f" ({info['model']})" if info["model"] else ""
        return (
            f"PAID API KEY REQUIRED{model_part}\n"
            "This model has quota limit 0 on the Free Tier. "
            "Switch to an available free model or provide a paid API key in Key settings."
        )

    if info["is_429"]:
        tier_label = "Free Tier " if info["is_free_tier"] else ""
        model_part = f" on {info['model']}" if info["model"] else ""
        limit_part = f" (limit: {info['quota_value']:,})" if info["quota_value"] is not None else ""
        metric_hint = ""
        low = f"{info['quota_id']} {info['quota_metric']}".lower()
        if "input" in low or "token" in low:
            metric_hint = "input tokens"
        elif "request" in low:
            metric_hint = "requests"

        scope_desc = f"{tier_label}{metric_hint + ' ' if metric_hint else ''}{'daily' if info['window'] == 'day' else 'per-minute' if info['window'] == 'minute' else ''} quota{limit_part}{model_part}".strip()
        if info["window"] == "day":
            advice = "Daily quota exhausted. Retry tomorrow, switch models, or use a paid API key."
        elif info["retry_seconds"]:
            advice = f"Wait ~{info['retry_seconds']}s and retry (or start a fresh session / batch tool calls to use fewer TPM)."
        else:
            advice = "Wait briefly and retry (or start a fresh session / batch tool calls)."
        return f"{info['headline']}\nExceeded {scope_desc}.\n{advice}"

    if info["is_auth"]:
        return f"INVALID API KEY\n{text}"

    if "thought_signature" in text.lower():
        return (
            "TOOL HISTORY SIGNATURE MISMATCH\n"
            "Switching model families mid-tool-loop can invalidate earlier thought signatures. "
            "Start a '+ New' session to continue with the new model cleanly.\n\n"
            f"Details: {text}"
        )

    return text or "Unknown Gemini API error."
