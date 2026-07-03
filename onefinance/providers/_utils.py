from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from onefinance._clock import get_clock


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


def normalize_symbol(symbol: str) -> str:
    """Canonical ticker form: stripped and upper-cased."""
    return symbol.strip().upper()


def parse_iso_date(value: str) -> date:
    """Parse a bare ISO date (``YYYY-MM-DD``).

    If a full datetime is passed in, the date portion is returned.
    """
    if len(value) > 10:
        return parse_iso_datetime_utc(value).date()
    return date.fromisoformat(value)


def parse_iso_datetime_utc(value: str) -> datetime:
    """Parse an ISO 8601 datetime into a UTC-aware ``datetime``.

    Accepts trailing ``Z``, explicit offsets, or naive datetimes (which are
    interpreted as UTC).
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def quarter_from_date(d: date) -> int:
    """Return the fiscal quarter (1–4) for a calendar date."""
    return (d.month - 1) // 3 + 1


def format_period(year: int | str, quarter: int | str | None = None) -> str:
    """Format a fiscal period label.

    ``format_period(2024)`` → ``"2024"``;
    ``format_period(2024, 3)`` → ``"2024-Q3"``;
    ``format_period(2024, "Q3")`` → ``"2024-Q3"``;
    ``format_period(2024, "FY")`` → ``"2024-FY"``.
    """
    if quarter is None or quarter == "":
        return str(year)
    if isinstance(quarter, int):
        return f"{year}-Q{quarter}"
    return f"{year}-{quarter}"


def utc_now() -> datetime:
    """UTC-aware ``datetime`` for the active :class:`Clock`."""
    return get_clock().now()


def change_pct_from_prev_close(price: float, prev_close: float | None) -> float | None:
    """Percent change vs *prev_close*, or ``None`` if unavailable.

    Derived directly from ``price``/``prev_close`` rather than trusting a
    provider's own change-percent field, so every provider reports it the
    same way regardless of raw field naming/rounding quirks.
    """
    if prev_close is None or prev_close <= 0:
        return None
    return round((price - prev_close) / prev_close * 100, 4)
