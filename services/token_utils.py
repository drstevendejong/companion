"""
services/token_utils.py - Zero-token local token estimation and smart output formatting.
"""

from __future__ import annotations

import math


def estimate_tokens(text: str) -> int:
    """
    Fast, deterministic token estimate (~3.6 chars/token for mixed code/prose).
    Used for pre-flight gate displays without spending API quota.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 3.6))


def estimate_image_tokens() -> int:
    """Standard Gemini tile estimate for a single UI screenshot."""
    return 258


def truncate_head_tail(text: str, max_chars: int = 16_000, head_ratio: float = 0.35) -> tuple[str, bool]:
    """Preserves both start and tail (usually where tracebacks/summaries live)."""
    if len(text) <= max_chars:
        return text, False
    head_len = int(max_chars * head_ratio)
    tail_len = max_chars - head_len
    omitted = len(text) - max_chars
    marker = f"\n\n... [{omitted:,} chars omitted from middle] ...\n\n"
    return f"{text[:head_len]}{marker}{text[-tail_len:]}", True
