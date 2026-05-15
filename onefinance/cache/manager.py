"""CacheManager — diskcache-backed, TTL-aware caching layer.

Sits between the ``OneFinanceClient`` and the provider router.
Stores JSON-serialised Pydantic models in a provider-agnostic way
so that swapping providers does not invalidate the cache.

See design doc §10 for the full specification.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, TypeVar

import diskcache

from onefinance.cache.keys import make_key
from onefinance.core.models import (
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    DCFValuation,
    EarningsRecord,
    FinancialRatios,
    FinanceModel,
    IncomeStatement,
    InsiderTrade,
    PriceBar,
    Quote,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=FinanceModel)

# ---------------------------------------------------------------------------
# Model registry — maps type name → class for deserialisation
# ---------------------------------------------------------------------------

_MODEL_REGISTRY: dict[str, type[FinanceModel]] = {
    "PriceBar": PriceBar,
    "Quote": Quote,
    "IncomeStatement": IncomeStatement,
    "BalanceSheet": BalanceSheet,
    "CashFlow": CashFlow,
    "CompanyInfo": CompanyInfo,
    "FinancialRatios": FinancialRatios,
    "EarningsRecord": EarningsRecord,
    "InsiderTrade": InsiderTrade,
    "DCFValuation": DCFValuation,
}

# ---------------------------------------------------------------------------
# Default TTLs (seconds) — per design doc §6 / §10
# ---------------------------------------------------------------------------

_TTL_QUOTE = 30                        # Type B — always current
_TTL_FINANCIALS = 7 * 24 * 3600        # Type A — 7 days
_TTL_INFO = 30 * 24 * 3600             # Type A — 30 days
_TTL_INSIDER_TRADES = 1 * 24 * 3600    # Type A — 1 day
_TTL_RATIOS_DEFAULT = 7 * 24 * 3600    # Type C fresh=False — 7 days
_TTL_RATIOS_FRESH = 1 * 3600           # Type C fresh=True — 1 hour
_TTL_EARNINGS_DEFAULT = 7 * 24 * 3600  # Type C fresh=False — 7 days
_TTL_EARNINGS_FRESH = 1 * 3600         # Type C fresh=True — 1 hour
_TTL_DCF = 7 * 24 * 3600               # Type A — 7 days

# Price history — smart TTL (computed per-request)
_TTL_PRICE_HISTORICAL = 30 * 24 * 3600   # fully historical — 30 days
_TTL_PRICE_MARKET_OPEN = 60               # today's bar still forming — 1 min
_TTL_PRICE_MARKET_CLOSED = 6 * 3600       # market closed, bar settled — 6 hours

# US market hours (NYSE) — Eastern Time
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)

# Default cache directory and size
_DEFAULT_CACHE_DIR = "~/.one_finance_data/cache"
_DEFAULT_SIZE_LIMIT_GB = 2


def is_market_open_now() -> bool:
    """Check if US equities market (NYSE) is currently open.

    v1 uses simplified logic: weekdays 9:30–16:00 ET.
    Does not account for NYSE holidays — a future version could
    integrate ``pandas_market_calendars``.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        # Python < 3.9 fallback (shouldn't happen with our >= 3.11 requirement)
        from datetime import timezone as _tz  # noqa: F811
        # Approximate ET as UTC-5 (ignores DST)
        import datetime as _dt

        now_utc = _dt.datetime.now(_tz.utc)
        et_offset = _dt.timedelta(hours=-5)
        now_et = now_utc + et_offset
        if now_et.weekday() >= 5:
            return False
        return _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)

    # Weekend check
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False

    return _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE


def ttl_for_price_history(start: date, end: date) -> int:
    """Compute smart TTL for price history requests.

    See design doc §10 — TTL is fully derivable from the date range:

    - ``end < today`` → 30 days (fully historical, near-immutable)
    - ``end >= today`` and market open → 1 minute (bar still forming)
    - ``end >= today`` and market closed → 6 hours (bar settled)
    """
    today = date.today()
    if end < today:
        return _TTL_PRICE_HISTORICAL
    if is_market_open_now():
        return _TTL_PRICE_MARKET_OPEN
    return _TTL_PRICE_MARKET_CLOSED


def default_ttl(endpoint: str, *, fresh: bool = False) -> int:
    """Return the default TTL for a given endpoint.

    Parameters
    ----------
    endpoint:
        One of the endpoint names (``"quote"``, ``"financials"``, etc.).
    fresh:
        For Type C endpoints, whether the caller requested fresh data.
    """
    match endpoint:
        case "quote":
            return _TTL_QUOTE
        case "financials":
            return _TTL_FINANCIALS
        case "info":
            return _TTL_INFO
        case "insider_trades":
            return _TTL_INSIDER_TRADES
        case "ratios":
            return _TTL_RATIOS_FRESH if fresh else _TTL_RATIOS_DEFAULT
        case "earnings":
            return _TTL_EARNINGS_FRESH if fresh else _TTL_EARNINGS_DEFAULT
        case "dcf":
            return _TTL_DCF
        case "news":
            return 3600
        case "corporate_actions":
            return 604800
        case "institutional_holders":
            return 604800
        case "analyst_data":
            return 14400
        case "forward_estimates":
            return 14400
        case "options_expirations":
            return 43200  # 12 hours
        case "option_chain":
            return 300  # 5 minutes
        case "screen_stocks":
            return 3600  # 1 hour
        case "sector_overview":
            return 86400  # 1 day
        case "price_history":
            # Caller should use ttl_for_price_history() directly
            # Fall back to 6 hours as a safe default
            return _TTL_PRICE_MARKET_CLOSED
        case _:
            return _TTL_PRICE_MARKET_CLOSED  # safe default


class CacheManager:
    """diskcache-backed cache with TTL-aware get/set and tag-based invalidation.

    Parameters
    ----------
    cache_dir:
        Directory for the diskcache SQLite store.
        Defaults to ``~/.one_finance_data/cache``.
    size_limit_gb:
        Maximum cache size in GB. LRU eviction when exceeded.
    """

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        size_limit_gb: float = _DEFAULT_SIZE_LIMIT_GB,
    ) -> None:
        resolved_dir = Path(cache_dir or _DEFAULT_CACHE_DIR).expanduser()
        self._cache = diskcache.Cache(
            str(resolved_dir),
            size_limit=int(size_limit_gb * 1024 ** 3),
        )

    def close(self) -> None:
        """Close the underlying diskcache store."""
        self._cache.close()

    # -------------------------------------------------------------------
    # Core get / set
    # -------------------------------------------------------------------

    def get(self, key: str) -> list[FinanceModel] | FinanceModel | None:
        """Retrieve a cached value by key.

        Returns ``None`` on miss.  On hit, deserialises the JSON
        envelope back into the appropriate Pydantic model(s).
        """
        raw = self._cache.get(key)
        if raw is None:
            return None

        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupt cache entry for key %s, ignoring", key)
            return None

        return _deserialise_envelope(envelope)

    def set(
        self,
        key: str,
        value: list[FinanceModel] | FinanceModel,
        ttl: int,
        tag: str | None = None,
    ) -> None:
        """Store a value with TTL and optional tag for bulk invalidation.

        Parameters
        ----------
        key:
            Cache key (from ``make_key``).
        value:
            A single model or a list of models.
        ttl:
            Time-to-live in seconds.
        tag:
            Tag for bulk invalidation (typically the endpoint name).
        """
        envelope = _serialise_envelope(value)
        self._cache.set(key, json.dumps(envelope), expire=ttl, tag=tag)

    # -------------------------------------------------------------------
    # Invalidation
    # -------------------------------------------------------------------

    def invalidate_by_type(self, data_type: str) -> int:
        """Remove all cache entries tagged with *data_type*.

        Returns the number of entries evicted.
        """
        # diskcache's evict() removes entries matching a tag
        return self._cache.evict(data_type)

    def clear(self) -> None:
        """Remove all entries from the cache."""
        self._cache.clear()

    # -------------------------------------------------------------------
    # Stats
    # -------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return cache statistics.

        Returns
        -------
        dict
            Keys: ``entries``, ``size_bytes``, ``size_mb``,
            ``size_limit_bytes``, ``hits``, ``misses``.
        """
        volume = self._cache.volume()
        hits, misses = 0, 0
        if hasattr(self._cache, "stats"):
            try:
                stat_result = self._cache.stats(enable=False)
                if isinstance(stat_result, tuple) and len(stat_result) >= 2:
                    hits, misses = stat_result[0], stat_result[1]
            except Exception:
                pass
        return {
            "entries": len(self._cache),
            "size_bytes": volume,
            "size_mb": round(volume / (1024 * 1024), 2),
            "size_limit_bytes": self._cache.size_limit,
            "hits": hits,
            "misses": misses,
        }

    # -------------------------------------------------------------------
    # Convenience helpers
    # -------------------------------------------------------------------

    def make_key(self, data_type: str, **params: Any) -> str:
        """Proxy to ``cache.keys.make_key`` for convenience."""
        return make_key(data_type, **params)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _serialise_envelope(
    value: list[FinanceModel] | FinanceModel,
) -> dict[str, Any]:
    """Wrap model(s) in a JSON envelope for storage.

    Envelope format::

        {"type": "PriceBar", "is_list": true, "data": [...]}
        {"type": "CompanyInfo", "is_list": false, "data": {...}}
    """
    if isinstance(value, list):
        if not value:
            return {"type": "empty", "is_list": True, "data": []}
        type_name = type(value[0]).__name__
        return {
            "type": type_name,
            "is_list": True,
            "data": [json.loads(item.model_dump_json()) for item in value],
        }
    else:
        type_name = type(value).__name__
        return {
            "type": type_name,
            "is_list": False,
            "data": json.loads(value.model_dump_json()),
        }


def _deserialise_envelope(
    envelope: dict[str, Any],
) -> list[FinanceModel] | FinanceModel | None:
    """Reconstruct model(s) from a cache envelope."""
    type_name = envelope.get("type")
    is_list = envelope.get("is_list", False)
    data = envelope.get("data")

    if type_name == "empty":
        return []

    model_cls = _MODEL_REGISTRY.get(type_name)  # type: ignore[arg-type]
    if model_cls is None:
        logger.warning("Unknown model type in cache: %s", type_name)
        return None

    if is_list:
        return [model_cls.model_validate(item) for item in data]
    else:
        return model_cls.model_validate(data)
