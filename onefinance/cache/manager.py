"""CacheManager — diskcache-backed, TTL-aware caching layer.

Sits between the ``OneFinanceClient`` and the provider router.
Stores JSON-serialised Pydantic models in a provider-agnostic way
so that swapping providers does not invalidate the cache.

See design doc §10 for the full specification.
"""

from __future__ import annotations

import json
import logging
import operator
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import date, time, timedelta
from pathlib import Path
from typing import Any, TypeVar, cast
from zoneinfo import ZoneInfo

import diskcache  # type: ignore[import-untyped]

import onefinance.core.models as _models  # noqa: F401  — ensure subclasses imported
from onefinance._clock import get_clock
from onefinance.cache.keys import make_key
from onefinance.core.models import FinanceModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=FinanceModel)

# ---------------------------------------------------------------------------
# Model registry — auto-populated from FinanceModel.__subclasses__()
# ---------------------------------------------------------------------------


def _all_finance_models() -> dict[str, type[FinanceModel]]:
    """Walk FinanceModel's full subclass tree once and index by class name."""
    out: dict[str, type[FinanceModel]] = {}
    stack: list[type[FinanceModel]] = list(FinanceModel.__subclasses__())
    while stack:
        cls = stack.pop()
        out[cls.__name__] = cls
        stack.extend(cls.__subclasses__())
    return out


_MODEL_REGISTRY: dict[str, type[FinanceModel]] = _all_finance_models()

# ---------------------------------------------------------------------------
# Default TTLs (seconds) — per design doc §6 / §10
# ---------------------------------------------------------------------------

_TTL_QUOTE_OPEN = 30  # Type B — live during market hours
_TTL_QUOTE_CLOSED = 2 * 60  # market closed same day — price barely moves
_TTL_QUOTE_WEEKEND = 30 * 60  # weekend / holiday — price static
_TTL_QUOTE = _TTL_QUOTE_OPEN  # table fallback (overridden at call site)
_TTL_FINANCIALS = 24 * 3600  # Type A — 1 day (key includes date, so daily boundary is TTL boundary)
_TTL_INFO = 30 * 24 * 3600  # Type A — 30 days
_TTL_INSIDER_TRADES = 1 * 24 * 3600  # Type A — 1 day
_TTL_RATIOS_DEFAULT = 24 * 3600  # Type C fresh=False — 1 day (price-sensitive; key includes date)
_TTL_RATIOS_FRESH = 1 * 3600  # Type C fresh=True — 1 hour
_TTL_EARNINGS_DEFAULT = 24 * 3600  # Type C fresh=False — 1 day (key includes date)
_TTL_EARNINGS_FRESH = 1 * 3600  # Type C fresh=True — 1 hour
_TTL_DCF = 7 * 24 * 3600  # Type A — 7 days

# Price history — smart TTL (computed per-request)
_TTL_PRICE_HISTORICAL = 30 * 24 * 3600  # fully historical — 30 days
_TTL_PRICE_MARKET_OPEN = 60  # today-only bar still forming — 1 min
_TTL_PRICE_MARKET_OPEN_HISTORICAL = 30 * 60  # multi-day range + today forming — 30 min
_TTL_PRICE_MARKET_CLOSED = 6 * 3600  # market closed, bar settled — 6 hours

# Option chain — market-aware TTL
_TTL_OPTION_CHAIN_OPEN = 5 * 60  # 5 min during market hours (active pricing)
_TTL_OPTION_CHAIN_CLOSED = 4 * 3600  # 4h after close / overnight (chain stable)

# US market hours (NYSE) — Eastern Time
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)
_ET = ZoneInfo("America/New_York")

# NYSE holidays (static table — update yearly).
# Sources: NYSE official holiday schedule for 2025–2026.
_NYSE_HOLIDAYS: frozenset[date] = frozenset(
    [
        # 2025
        date(2025, 1, 1),  # New Year's Day
        date(2025, 1, 20),  # MLK Day
        date(2025, 2, 17),  # Presidents' Day
        date(2025, 4, 18),  # Good Friday
        date(2025, 5, 26),  # Memorial Day
        date(2025, 6, 19),  # Juneteenth
        date(2025, 7, 4),  # Independence Day
        date(2025, 9, 1),  # Labor Day
        date(2025, 11, 27),  # Thanksgiving
        date(2025, 12, 25),  # Christmas
        # 2026
        date(2026, 1, 1),  # New Year's Day
        date(2026, 1, 19),  # MLK Day
        date(2026, 2, 16),  # Presidents' Day
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),  # Independence Day (observed)
        date(2026, 9, 7),  # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
    ]
)

# Default cache directory and size
_DEFAULT_CACHE_DIR = "~/.one_finance_data/cache"
_DEFAULT_SIZE_LIMIT_GB = 2

# In-process memo layer (P5) — bounded independently of the real cache TTL so
# it can only ever narrow, never widen, the staleness window a caller sees.
_MEMO_MAX_ENTRIES = 256
_MEMO_MAX_TTL_S = 5.0
_MEMO_MISS = object()


def _is_trading_day(d: date) -> bool:
    """Return True if *d* is a NYSE trading day (not weekend or NYSE holiday)."""
    return d.weekday() < 5 and d not in _NYSE_HOLIDAYS


def _has_trading_days_in_gap(after: date, up_to: date) -> bool:
    """Return True if any NYSE trading day exists in the open-closed interval ``(after, up_to]``.

    Used by the price-range subsumption check: a cached ``[s, after]`` range can be
    treated as covering ``[s, up_to]`` when this returns False (no new bars can exist).
    The interval is bounded to ``up_to <= date.today()`` by the caller.
    """
    d = after + timedelta(days=1)
    while d <= up_to:
        if _is_trading_day(d):
            return True
        d += timedelta(days=1)
    return False


def is_market_open_now() -> bool:
    """Return True if US equities market (NYSE) is currently open.

    Uses DST-correct Eastern Time via ``zoneinfo`` (stdlib on Python 3.9+).
    Accounts for weekends and the NYSE holiday schedule.
    """
    now_et = get_clock().now().astimezone(_ET)

    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if now_et.date() in _NYSE_HOLIDAYS:
        return False

    return _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE


def ttl_for_price_history(start: date, end: date) -> int:
    """Compute smart TTL for price history requests.

    - ``end < today`` → 30 days (fully historical, near-immutable)
    - ``end >= today`` and market open and range > 1 day → 30 min
      (historical bars are settled; only today's bar is live, so a
      1-min TTL for a 1-year request is needlessly wasteful)
    - ``end >= today`` and market open and range == 0-1 days → 1 min
      (today-only: the bar is still forming and staleness matters)
    - ``end >= today`` and market closed → 6 hours (bar settled)
    """
    today = date.today()
    if end < today:
        return _TTL_PRICE_HISTORICAL
    if is_market_open_now():
        if (end - start).days > 1:
            return _TTL_PRICE_MARKET_OPEN_HISTORICAL
        return _TTL_PRICE_MARKET_OPEN
    return _TTL_PRICE_MARKET_CLOSED


def ttl_for_quote() -> int:
    """Market-aware TTL for quotes.

    - Market open: 30s (live price)
    - Market closed (weekday): 2 min (price settled, may tick in after-hours)
    - Weekend / holiday: 30 min (price static until next open)
    """
    now_et = get_clock().now().astimezone(_ET)
    is_closed_day = now_et.weekday() >= 5 or now_et.date() in _NYSE_HOLIDAYS
    if is_closed_day:
        return _TTL_QUOTE_WEEKEND
    if is_market_open_now():
        return _TTL_QUOTE_OPEN
    return _TTL_QUOTE_CLOSED


def ttl_for_option_chain() -> int:
    """Market-aware TTL for option chains.

    Options are actively repriced during market hours (5 min), but the
    chain structure (strikes, expiries) is stable after close (2 hours).
    """
    return _TTL_OPTION_CHAIN_OPEN if is_market_open_now() else _TTL_OPTION_CHAIN_CLOSED


_DEFAULT_TTLS: dict[str, int] = {
    "quote": _TTL_QUOTE,
    "financials": _TTL_FINANCIALS,
    "info": _TTL_INFO,
    "insider_trades": _TTL_INSIDER_TRADES,
    "ratios": _TTL_RATIOS_DEFAULT,
    "earnings": _TTL_EARNINGS_DEFAULT,
    "dcf": _TTL_DCF,
    "news": 3600,
    "corporate_actions": 604800,
    "institutional_holders": 604800,
    "analyst_data": 14400,
    "forward_estimates": 86400,  # estimates update at most daily, often weekly
    "options_expirations": 43200,
    "option_chain": 300,  # fallback only; use ttl_for_option_chain() at call site
    "screen_stocks": 3600,
    "sector_overview": 86400,
    "earnings_calendar": 14400,
    "economic_calendar": 14400,  # macro releases update at most a few times/day
    "price_history": _TTL_PRICE_MARKET_CLOSED,
    "short_interest": 86400,
    "market_sentiment": 14400,
    "peers": 7 * 24 * 3600,
}

_FRESH_TTLS: dict[str, int] = {
    "ratios": _TTL_RATIOS_FRESH,
    "earnings": _TTL_EARNINGS_FRESH,
}


def default_ttl(
    endpoint: str,
    *,
    fresh: bool = False,
    overrides: dict[str, int] | None = None,
) -> int:
    """Return the default TTL for a given endpoint.

    Parameters
    ----------
    endpoint:
        One of the endpoint names (``"quote"``, ``"financials"``, etc.).
    fresh:
        For Type C endpoints, whether the caller requested fresh data.
    overrides:
        Per-endpoint TTL overrides (typically ``config.cache.ttl_overrides``).
        An override always wins, regardless of ``fresh``.
    """
    if overrides and endpoint in overrides:
        return int(overrides[endpoint])
    if fresh and endpoint in _FRESH_TTLS:
        return _FRESH_TTLS[endpoint]
    return _DEFAULT_TTLS.get(endpoint, _TTL_PRICE_MARKET_CLOSED)


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
            size_limit=int(size_limit_gb * 1024**3),
            statistics=1,
        )
        # In-process memo layer (P5): repeated get() calls for the same key within
        # a single process — e.g. get_quotes fanning out over a watchlist, or
        # get_indicators re-reading bars it just fetched — skip the diskcache disk
        # read + json.loads + Pydantic model_validate round trip entirely. Bounded
        # both in size and in staleness window (independent of, and much shorter
        # than, the real cache TTL) so it never meaningfully changes what data a
        # caller can observe versus talking to diskcache directly.
        self._memo: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def close(self) -> None:
        """Close the underlying diskcache store."""
        self._cache.close()

    # -------------------------------------------------------------------
    # Core get / set
    # -------------------------------------------------------------------

    def get(self, key: str) -> list[FinanceModel] | FinanceModel | list[date] | None:
        """Retrieve a cached value by key.

        Returns ``None`` on miss.  On hit, deserialises the JSON
        envelope back into the appropriate Pydantic model(s).
        """
        memo_value = self._memo_get(key)
        if memo_value is not _MEMO_MISS:
            return cast(
                "list[FinanceModel] | FinanceModel | list[date] | None",
                memo_value,
            )

        raw = self._cache.get(key)
        if raw is None:
            return None

        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupt cache entry for key %s, ignoring", key)
            return None

        value = _deserialise_envelope(envelope)
        self._memo_put(key, value)
        return value

    def set(
        self,
        key: str,
        value: Sequence[FinanceModel] | FinanceModel | Sequence[date],
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
        self._memo_put(key, value, ttl=ttl)

    # -------------------------------------------------------------------
    # In-process memo layer (P5)
    # -------------------------------------------------------------------

    def _memo_get(self, key: str) -> Any:
        entry = self._memo.get(key)
        if entry is None:
            return _MEMO_MISS
        expires_at, value = entry
        if get_clock().perf_counter() >= expires_at:
            del self._memo[key]
            return _MEMO_MISS
        self._memo.move_to_end(key)
        return value

    def _memo_put(self, key: str, value: Any, ttl: int | None = None) -> None:
        local_ttl = _MEMO_MAX_TTL_S if ttl is None else min(ttl, _MEMO_MAX_TTL_S)
        if local_ttl <= 0:
            return
        self._memo[key] = (get_clock().perf_counter() + local_ttl, value)
        self._memo.move_to_end(key)
        if len(self._memo) > _MEMO_MAX_ENTRIES:
            self._memo.popitem(last=False)

    # -------------------------------------------------------------------
    # Invalidation
    # -------------------------------------------------------------------

    def invalidate_by_type(self, data_type: str) -> int:
        """Remove all cache entries tagged with *data_type*.

        Also evicts the matching last-known-good copies (tagged
        ``lkg:{data_type}``) — an explicit invalidation means the data is
        known stale/changed, so it must not survive as a stale-on-error
        fallback and resurface later. Returns the total number of entries
        evicted across both tags.
        """
        # diskcache's evict() removes entries matching a tag
        evicted: int = self._cache.evict(data_type)
        evicted += self._cache.evict(f"{self._LKG_PREFIX}:{data_type}")
        # Tag-based eviction doesn't tell us which keys it touched, and the memo
        # layer isn't tag-indexed — drop it wholesale (cheap; capped at 256
        # entries) rather than risk serving a value invalidation just removed.
        self._memo.clear()
        return evicted

    def clear(self) -> None:
        """Remove all entries from the cache."""
        self._cache.clear()
        self._memo.clear()

    # -------------------------------------------------------------------
    # Negative (not-supported) cache
    # -------------------------------------------------------------------

    _NEG_PREFIX = "not_supported"
    _NEG_TTL = 86400  # 24 h — plan restrictions rarely change within a day

    def get_negative(self, provider: str, endpoint: str, symbol: str | None) -> bool:
        """Return True if (provider, endpoint, symbol) is cached as not_supported."""
        key = f"{self._NEG_PREFIX}:{provider}:{endpoint}:{(symbol or '').upper()}"
        return bool(self._cache.get(key))

    def set_negative(
        self,
        provider: str,
        endpoint: str,
        symbol: str | None,
        ttl: int = _NEG_TTL,
    ) -> None:
        """Mark (provider, endpoint, symbol) as not_supported for *ttl* seconds."""
        key = f"{self._NEG_PREFIX}:{provider}:{endpoint}:{(symbol or '').upper()}"
        self._cache.set(key, True, expire=ttl)

    def get_negative_global(self, provider: str, endpoint: str) -> bool:
        """Return True if *provider*/*endpoint* is plan-gated (402/403) for ALL symbols.

        Unlike ``get_negative``, this is keyed without a symbol and fires for any
        call to the provider+endpoint regardless of symbol.
        """
        key = f"{self._NEG_PREFIX}:{provider}:{endpoint}:"
        return bool(self._cache.get(key))

    def set_negative_global(
        self,
        provider: str,
        endpoint: str,
        ttl: int = _NEG_TTL,
    ) -> None:
        """Mark *provider*/*endpoint* as plan-gated for *ttl* seconds (symbol-independent)."""
        key = f"{self._NEG_PREFIX}:{provider}:{endpoint}:"
        self._cache.set(key, True, expire=ttl)

    # -------------------------------------------------------------------
    # Augment-filler cache (P2-A)
    # -------------------------------------------------------------------
    # Caches the secondary provider result used to fill missing augment
    # fields (e.g. volume) so repeated short-TTL primary fetches don't
    # each pay a round-trip to the augment provider.

    _AUG_PREFIX = "augment"
    _AUG_TTL = 5 * 60  # 5 minutes — volume moves slowly vs. 30s quote TTL

    def get_augment(self, endpoint: str, symbol: str) -> Any | None:
        """Retrieve a cached augment-filler result for *symbol*/*endpoint*."""
        key = f"{self._AUG_PREFIX}:{endpoint}:{symbol.upper()}"
        raw = self._cache.get(key)
        if raw is None:
            return None
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return _deserialise_envelope(envelope)

    def set_augment(self, endpoint: str, symbol: str, value: Any, ttl: int = _AUG_TTL) -> None:
        """Store an augment-filler result for *symbol*/*endpoint*."""
        key = f"{self._AUG_PREFIX}:{endpoint}:{symbol.upper()}"
        envelope = _serialise_envelope(value)
        self._cache.set(key, json.dumps(envelope), expire=ttl)

    # -------------------------------------------------------------------
    # Router cooldown state persistence (P3)
    # -------------------------------------------------------------------
    # Serialises ProviderState to diskcache so cooldown backoff accumulates
    # across CLI process restarts.

    _ROUTER_STATE_PREFIX = "router_state"
    _ROUTER_STATE_TTL = 4 * 3600  # 4 h max — aligned with max_backoff default

    def get_router_state(self, provider: str) -> dict[str, Any] | None:
        """Load persisted cooldown state for *provider* (None if absent/expired)."""
        key = f"{self._ROUTER_STATE_PREFIX}:{provider}"
        raw = self._cache.get(key)
        if not isinstance(raw, dict):
            return None
        return raw

    def set_router_state(
        self,
        provider: str,
        state: dict[str, Any],
        ttl: int = _ROUTER_STATE_TTL,
    ) -> None:
        """Persist cooldown state for *provider* with *ttl* seconds TTL."""
        key = f"{self._ROUTER_STATE_PREFIX}:{provider}"
        self._cache.set(key, state, expire=ttl)

    # -------------------------------------------------------------------
    # Last-known-good (stale-on-error) cache
    # -------------------------------------------------------------------
    #
    # On every successful fetch the client dual-writes a long-lived copy of
    # the result under an ``lkg:`` prefix. When every provider fails, the
    # client serves this copy instead of raising — boosting availability.
    # The LKG TTL bounds the maximum staleness: an expired entry is simply
    # gone, so the error propagates as normal and stale data is never
    # served past its TTL.

    _LKG_PREFIX = "lkg"

    @classmethod
    def _lkg_key(cls, cache_key: str) -> str:
        return f"{cls._LKG_PREFIX}:{cache_key}"

    def set_last_known_good(
        self,
        cache_key: str,
        value: Sequence[FinanceModel] | FinanceModel | Sequence[date],
        ttl: int,
        tag: str | None = None,
    ) -> None:
        """Store a long-lived last-known-good copy keyed off *cache_key*."""
        lkg_tag = f"{self._LKG_PREFIX}:{tag}" if tag else self._LKG_PREFIX
        self.set(self._lkg_key(cache_key), value, ttl=ttl, tag=lkg_tag)

    def get_last_known_good(
        self, cache_key: str
    ) -> list[FinanceModel] | FinanceModel | list[date] | None:
        """Retrieve the last-known-good copy for *cache_key* (None if absent/expired)."""
        return self.get(self._lkg_key(cache_key))

    # -------------------------------------------------------------------
    # Stats
    # -------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return cache statistics.

        Returns
        -------
        dict
            Keys: ``entries``, ``size_bytes``, ``size_mb``,
            ``size_limit_bytes``, ``hits``, ``misses``, ``hit_rate``.
            ``hit_rate`` is the lifetime ratio (hits / (hits + misses)),
            persisted in the diskcache SQLite store across processes.
        """
        volume = self._cache.volume()
        hits, misses = 0, 0
        if hasattr(self._cache, "stats"):
            try:
                stat_result = self._cache.stats(enable=True)
                if isinstance(stat_result, tuple) and len(stat_result) >= 2:
                    hits, misses = stat_result[0], stat_result[1]
            except Exception:
                pass
        total = hits + misses
        hit_rate = round(hits / total, 3) if total > 0 else 0.0
        return {
            "entries": len(self._cache),
            "size_bytes": volume,
            "size_mb": round(volume / (1024 * 1024), 2),
            "size_limit_bytes": self._cache.size_limit,
            "hits": hits,
            "misses": misses,
            "hit_rate": hit_rate,
        }

    # -------------------------------------------------------------------
    # Convenience helpers
    # -------------------------------------------------------------------

    def make_key(self, data_type: str, **params: Any) -> str:
        """Proxy to ``cache.keys.make_key`` for convenience."""
        return make_key(data_type, **params)

    # -------------------------------------------------------------------
    # Price-history range subsumption
    # -------------------------------------------------------------------
    #
    # Overlapping date-range requests (e.g. ``1y`` then ``6mo``, or the
    # 180-day window ``get_indicators`` uses) would each be a distinct
    # cache key and thus a fresh provider call.  To avoid that, every
    # stored price-history range is registered in a small per-(symbol,
    # interval) index.  A later request whose range is fully contained in
    # an already-cached range is served by slicing the superset — no API
    # call.  The index degrades gracefully: a stale or evicted entry just
    # falls through to a normal fetch (status-quo behaviour).

    _RANGE_INDEX_TTL = 30 * 24 * 3600  # match fully-historical price TTL
    _RANGE_INDEX_MAX = 64  # cap entries per (symbol, interval)

    @staticmethod
    def _range_index_key(symbol: str, interval: str) -> str:
        return f"price_index:{symbol.upper()}:{interval}"

    def find_covering_price_range(
        self, symbol: str, interval: str, start: date, end: date
    ) -> list[Any] | None:
        """Return cached bars sliced to ``[start, end]`` if a superset is cached."""
        return self._find_covering_range(
            self._range_index_key(symbol, interval),
            start,
            end,
            lambda items: _slice_range_items(items, start, end),
        )

    def find_extendable_price_range(
        self, symbol: str, interval: str, start: date, end: date
    ) -> tuple[list[Any], date, str] | None:
        """Find a cached range ``[start, e_end]`` that partially overlaps ``[start, end]``.

        Returns ``(bars, e_end, cache_key)`` when a partial overlap is found and there
        are actual trading days in the gap ``(e_end, end]`` that need fetching.  The
        caller should fetch ``[e_end + 1 day, end]`` from a provider, merge with *bars*,
        then call ``extend_price_range`` to update the cache.

        Returns ``None`` when no suitable partial overlap exists (fall through to a full fetch).
        """
        index_key = self._range_index_key(symbol, interval)
        raw = self._cache.get(index_key)
        if not raw:
            return None
        try:
            entries = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        today = date.today()
        for entry in entries:
            try:
                e_start = date.fromisoformat(entry["start"])
                e_end = date.fromisoformat(entry["end"])
                e_key = entry["key"]
            except (KeyError, TypeError, ValueError):
                continue
            # Partial overlap: same start, cached end is before requested end,
            # and the gap (e_end, end] contains at least one trading day.
            if e_start != start:
                continue
            if e_end >= end:
                continue  # already fully covered (handled by find_covering_price_range)
            if end > today:
                continue  # do not delta-fetch into the future
            if not _has_trading_days_in_gap(e_end, end):
                continue  # gap is all non-trading days; covering check handles this
            cached = self.get(e_key)
            if cached is None:
                continue  # evicted
            bars = cached if isinstance(cached, list) else [cached]
            return bars, e_end, e_key
        return None

    def extend_price_range(
        self,
        symbol: str,
        interval: str,
        original_start: date,
        original_end: date,
        new_end: date,
        original_key: str,
        all_bars: list[Any],
        ttl: int,
    ) -> None:
        """Merge new tail bars into the existing cache entry and update the range index.

        Called after a successful delta-fetch.  *all_bars* must be the fully merged
        bar list (existing cached bars + newly fetched bars), sorted by date.
        The old range-index entry is replaced with the extended ``[original_start, new_end]``
        entry pointing at the same *original_key*.
        """
        # Overwrite the existing cache entry with the merged bar list
        self.set(original_key, all_bars, ttl=ttl, tag="price_history")
        # Re-register the extended range in the index (replaces old entry for this key)
        self._record_range_index(
            self._range_index_key(symbol, interval),
            original_start,
            new_end,
            original_key,
            self._RANGE_INDEX_TTL,
            self._RANGE_INDEX_MAX,
        )

    def record_price_range(
        self, symbol: str, interval: str, start: date, end: date, key: str
    ) -> None:
        """Register a stored price-history range for later subsumption."""
        self._record_range_index(
            self._range_index_key(symbol, interval),
            start,
            end,
            key,
            self._RANGE_INDEX_TTL,
            self._RANGE_INDEX_MAX,
        )

    # -------------------------------------------------------------------
    # Calendar range subsumption (earnings_calendar, economic_calendar)
    # -------------------------------------------------------------------

    # Index lives 7 days — intentionally longer than the 4h entry TTL so
    # the index stays warm across multiple entry refreshes.
    _CALENDAR_INDEX_TTL = 7 * 24 * 3600
    _CALENDAR_INDEX_MAX = 32

    @staticmethod
    def _calendar_index_key(calendar_type: str) -> str:
        return f"calendar_index:{calendar_type}"

    def find_covering_calendar_range(
        self, calendar_type: str, start: date, end: date, date_attr: str
    ) -> list[Any] | None:
        """Return cached entries sliced to ``[start, end]`` if a superset is cached.

        *date_attr* is the attribute name on each entry that holds its date
        (``"report_date"`` for EarningsCalendarEntry, ``"event_date"`` for EconomicEvent).
        Returns ``None`` if no covering range is found.
        """
        return self._find_covering_range(
            self._calendar_index_key(calendar_type),
            start,
            end,
            lambda items: _slice_range_items(items, start, end, date_attr),
        )

    def record_calendar_range(self, calendar_type: str, start: date, end: date, key: str) -> None:
        """Register a stored calendar range for later subsumption."""
        self._record_range_index(
            self._calendar_index_key(calendar_type),
            start,
            end,
            key,
            self._CALENDAR_INDEX_TTL,
            self._CALENDAR_INDEX_MAX,
        )

    # -------------------------------------------------------------------
    # Private: generic range-index helpers shared by price + calendar
    # -------------------------------------------------------------------

    def _find_covering_range(
        self,
        index_key: str,
        start: date,
        end: date,
        slicer: Callable[[list[Any]], list[Any]],
    ) -> list[Any] | None:
        raw = self._cache.get(index_key)
        if not raw:
            return None
        try:
            entries = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        today = date.today()
        for entry in entries:
            try:
                e_start = date.fromisoformat(entry["start"])
                e_end = date.fromisoformat(entry["end"])
                e_key = entry["key"]
            except (KeyError, TypeError, ValueError):
                continue
            # A cached range [e_start, e_end] covers [start, end] when:
            #   - e_start <= start (covers the beginning)
            #   - either e_end >= end  (exact or superset)
            #     OR the gap (e_end, end] contains no trading days AND
            #        end <= today (future ranges are not pre-coverable)
            if e_start > start:
                continue
            if e_end < end:
                if end > today:
                    continue  # cannot cover future trading days
                if _has_trading_days_in_gap(e_end, end):
                    continue  # genuine gap — new bars may exist
            cached = self.get(e_key)
            if cached is None:
                continue  # expired / evicted — keep looking
            items = cached if isinstance(cached, list) else [cached]
            return slicer(items)
        return None

    def _record_range_index(
        self,
        index_key: str,
        start: date,
        end: date,
        key: str,
        ttl: int,
        max_entries: int,
    ) -> None:
        new_entry = {"start": start.isoformat(), "end": end.isoformat(), "key": key}
        with self._cache.transact():
            raw = self._cache.get(index_key)
            entries: list[dict[str, str]] = []
            if raw:
                try:
                    entries = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    entries = []
            entries = [e for e in entries if e.get("key") != key]
            entries.append(new_entry)
            if len(entries) > max_entries:
                entries = entries[-max_entries:]
            self._cache.set(index_key, json.dumps(entries), expire=ttl)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _slice_range_items(
    items: Sequence[Any], start: date, end: date, attr: str = "date"
) -> list[Any]:
    """Return items whose *attr* date falls within ``[start, end]`` inclusive."""
    getter = operator.attrgetter(attr)
    return [item for item in items if start <= getter(item) <= end]


def _serialise_envelope(
    value: Sequence[FinanceModel] | FinanceModel | Sequence[date],
) -> dict[str, Any]:
    """Wrap model(s) in a JSON envelope for storage.

    Envelope format::

        {"type": "PriceBar", "is_list": true, "data": [...]}
        {"type": "CompanyInfo", "is_list": false, "data": {...}}
        {"type": "__date_list__", "is_list": true, "data": ["2024-01-19", ...]}
    """
    if isinstance(value, list):
        if not value:
            return {"type": "empty", "is_list": True, "data": []}
        first = value[0]
        # Handle lists of plain Python date objects (e.g. options expirations)
        if isinstance(first, date) and type(first) is date:
            return {
                "type": "__date_list__",
                "is_list": True,
                "data": [d.isoformat() for d in value],
            }
        type_name = type(first).__name__
        return {
            "type": type_name,
            "is_list": True,
            "data": [item.model_dump(mode="json") for item in value],
        }
    else:
        type_name = type(value).__name__
        return {
            "type": type_name,
            "is_list": False,
            "data": value.model_dump(mode="json"),  # type: ignore[union-attr]
        }


def _deserialise_envelope(
    envelope: dict[str, Any],
) -> list[FinanceModel] | FinanceModel | list[date] | None:
    """Reconstruct model(s) from a cache envelope."""
    type_name = envelope.get("type")
    is_list = envelope.get("is_list", False)
    data = envelope.get("data")

    if type_name == "empty":
        return []

    # Special primitive: list of date objects
    if type_name == "__date_list__":
        return [date.fromisoformat(s) for s in (data or [])]

    model_cls = _MODEL_REGISTRY.get(type_name)  # type: ignore[arg-type]
    if model_cls is None:
        logger.warning("Unknown model type in cache: %s", type_name)
        return None

    if is_list:
        return [model_cls.model_validate(item) for item in (data or [])]
    return model_cls.model_validate(data)
