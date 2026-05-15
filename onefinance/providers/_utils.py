from __future__ import annotations

from typing import Any


def _safe_float(value: Any) -> float | None:
    """Convert to float, returning None on failure or None input."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _safe_int(value: Any) -> int | None:
    """Convert to int, returning None on failure or None input."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
