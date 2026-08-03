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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
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
_TTL_PRICE_MARKET_CLOSED = 6 * 3600  # market closed, bar settled — floor, see below

# Closed-market TTLs extend to the next opening bell: a settled daily series and
# a static price cannot change until the next session prints, so expiring at 6h
# (price history) or 30 min (weekend quote) just re-fetches identical bytes —
# audit showed 18 exact-key price_history refetches with the market closed.
# Bounded so a stale holiday table can only over-hold by ~3 days.
_TTL_CLOSED_MAX = 3 * 24 * 3600

# Option chain — market-aware TTL
_TTL_OPTION_CHAIN_OPEN = 5 * 60  # 5 min during market hours (active pricing)
_TTL_OPTION_CHAIN_CLOSED = 4 * 3600  # 4h after close / overnight (chain stable)

# US market hours (NYSE) — Eastern Time
_PREMARKET_OPEN = time(4, 0)  # first extended session — overnight adjustments land by here
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


@dataclass(frozen=True, slots=True)
class _RangeIndexEntry:
    """Typed representation of one cached date-range index row."""

    start: date
    end: date
    key: str

    @classmethod
    def from_raw(cls, raw: object) -> _RangeIndexEntry | None:
        if not isinstance(raw, dict):
            return None
        try:
            start_raw = raw["start"]
            end_raw = raw["end"]
            key = raw["key"]
        except KeyError:
            return None
        if not isinstance(start_raw, str) or not isinstance(end_raw, str):
            return None
        if not isinstance(key, str):
            return None
        try:
            return cls(
                start=date.fromisoformat(start_raw),
                end=date.fromisoformat(end_raw),
                key=key,
            )
        except ValueError:
            return None

    def to_raw(self) -> dict[str, str]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "key": self.key,
        }


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


def seconds_until_next_market_open() -> int:
    """Seconds from now until the next session boundary (0 if open now).

    While the market is closed, a settled daily series and a last-trade price
    cannot change until the next session — so a closed-market TTL that expires
    before then buys nothing but API calls.

    The boundary is the **pre-market open (04:00 ET), not the 09:30 bell**:
    Yahoo applies split and dividend adjustments to the whole historical series
    overnight ahead of an ex-date, so an entry held to 09:30 would serve
    unadjusted ``adj_close`` values to any pre-market request — and a 4:1 split
    silently throws every indicator computed off it. Inside the pre-market
    window the next boundary is that day's bell, so an entry written at 05:00
    cannot span the session it precedes. Capped at ``_TTL_CLOSED_MAX`` so a
    stale holiday table can only ever over-hold by a bounded amount.
    """
    if is_market_open_now():
        return 0

    now_et = get_clock().now().astimezone(_ET)
    today = now_et.date()
    today_trades = today.weekday() < 5 and today not in _NYSE_HOLIDAYS

    if today_trades and now_et.time() < _PREMARKET_OPEN:
        boundary = datetime.combine(today, _PREMARKET_OPEN, tzinfo=_ET)
    elif today_trades and now_et.time() < _MARKET_OPEN:
        boundary = datetime.combine(today, _MARKET_OPEN, tzinfo=_ET)
    else:
        probe = today + timedelta(days=1)
        while probe.weekday() >= 5 or probe in _NYSE_HOLIDAYS:
            probe += timedelta(days=1)
        boundary = datetime.combine(probe, _PREMARKET_OPEN, tzinfo=_ET)

    delta = int((boundary - now_et).total_seconds())
    return max(0, min(delta, _TTL_CLOSED_MAX))


def ttl_for_price_history(start: date, end: date) -> int:
    """Compute smart TTL for price history requests.

    - ``end < today`` → 30 days (fully historical, near-immutable)
    - ``end >= today`` and market open and range > 1 day → 30 min
      (historical bars are settled; only today's bar is live, so a
      1-min TTL for a 1-year request is needlessly wasteful)
    - ``end >= today`` and market open and range == 0-1 days → 1 min
      (today-only: the bar is still forming and staleness matters)
    - ``end >= today`` and market closed → until the next opening bell, floored
      at 6 hours (the bar is settled and cannot change before then)
    """
    today = date.today()
    if end < today:
        return _TTL_PRICE_HISTORICAL
    if is_market_open_now():
        if (end - start).days > 1:
            return _TTL_PRICE_MARKET_OPEN_HISTORICAL
        return _TTL_PRICE_MARKET_OPEN
    return max(_TTL_PRICE_MARKET_CLOSED, seconds_until_next_market_open())


def ttl_for_quote() -> int:
    """Market-aware TTL for quotes.

    - Market open: 30s (live price)
    - Market closed (weekday): 2 min (price settled, may tick in after-hours)
    - Weekend / holiday: until the next opening bell, floored at 30 min — the
      price is static, and there is no extended session to tick it
    """
    now_et = get_clock().now().astimezone(_ET)
    is_closed_day = now_et.weekday() >= 5 or now_et.date() in _NYSE_HOLIDAYS
    if is_closed_day:
        return max(_TTL_QUOTE_WEEKEND, seconds_until_next_market_open())
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
    "treasury_rates": 7 * 24 * 3600,
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

    def list_global_negatives(self) -> list[tuple[str, str]]:
        """Return all live (provider, endpoint) pairs benched by a global 402/403.

        Used by ``providers check`` to surface plan-gated endpoints — a
        provider that looks configured but silently skips every call.
        """
        out: list[tuple[str, str]] = []
        prefix = f"{self._NEG_PREFIX}:"
        for key in self._cache.iterkeys():
            if (
                isinstance(key, str)
                and key.startswith(prefix)
                and key.endswith(":")
                and self._cache.get(key)
            ):
                _, provider, endpoint, _ = key.split(":", 3)
                out.append((provider, endpoint))
        return sorted(out)

    # -------------------------------------------------------------------
    # Endpoint-ok marker (recent-success evidence)
    # -------------------------------------------------------------------
    # A 402/403 can be plan-gated per *symbol* (e.g. Finnhub free tier 403s
    # international listings while US quotes work fine), not per endpoint.
    # A recent success on the same (provider, endpoint) is evidence the
    # endpoint itself is available on this plan, so the router uses this
    # marker to veto the symbol-independent global bench and fall back to
    # the per-symbol negative entry instead.

    _OK_PREFIX = "endpoint_ok"
    # Longer than _NEG_TTL on purpose: if the marker expired daily, a day
    # whose *first* call hits a gated symbol (e.g. an index or international
    # listing) would re-bench the whole endpoint before any success could
    # refresh the marker. A week of evidence survives weekends and holidays;
    # the cost of a stale marker after a real plan downgrade is only one
    # extra probe per new symbol (per-symbol negatives still apply).
    _OK_TTL = 7 * 86400

    def get_endpoint_ok(self, provider: str, endpoint: str) -> bool:
        """Return True if *provider*/*endpoint* succeeded within the marker TTL."""
        return bool(self._cache.get(f"{self._OK_PREFIX}:{provider}:{endpoint}"))

    def mark_endpoint_ok(self, provider: str, endpoint: str, ttl: int = _OK_TTL) -> None:
        """Record a successful call for *provider*/*endpoint* and heal any global bench."""
        self._cache.set(f"{self._OK_PREFIX}:{provider}:{endpoint}", True, expire=ttl)
        self._cache.delete(f"{self._NEG_PREFIX}:{provider}:{endpoint}:")

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

    def get_router_state(self, provider: str) -> Mapping[str, object] | None:
        """Load persisted cooldown state for *provider* (None if absent/expired)."""
        key = f"{self._ROUTER_STATE_PREFIX}:{provider}"
        raw = self._cache.get(key)
        if not isinstance(raw, dict):
            return None
        return raw

    def set_router_state(
        self,
        provider: str,
        state: Mapping[str, object],
        ttl: int = _ROUTER_STATE_TTL,
    ) -> None:
        """Persist cooldown state for *provider* with *ttl* seconds TTL."""
        key = f"{self._ROUTER_STATE_PREFIX}:{provider}"
        self._cache.set(key, dict(state), expire=ttl)

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
        """Find the newest cached prefix that overlaps ``[start, end]``.

        The cached range may start before the requested range, which allows
        rolling windows (for example yesterday's 1-year window followed by
        today's) to reuse their overlap. Returns bars sliced to the requested
        start, the cached end, and the source cache key.

        Returns ``None`` when no suitable partial overlap exists (fall through to a full fetch).
        """
        index_key = self._range_index_key(symbol, interval)
        today = date.today()
        entries = sorted(
            self._load_range_index(index_key),
            key=lambda entry: (entry.end, -entry.start.toordinal()),
            reverse=True,
        )
        for entry in entries:
            # The cached range must cover the requested start and end before
            # the requested end, leaving only a tail to fetch.
            if entry.start > start or entry.end < start:
                continue
            if entry.end >= end:
                continue  # already fully covered (handled by find_covering_price_range)
            if end > today:
                continue  # do not delta-fetch into the future
            if not _has_trading_days_in_gap(entry.end, end):
                continue  # gap is all non-trading days; covering check handles this
            cached = self.get(entry.key)
            if cached is None:
                continue  # evicted
            bars = cached if isinstance(cached, list) else [cached]
            sliced = _slice_range_items(bars, start, entry.end)
            return sliced, entry.end, entry.key
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
        destination_key: str | None = None,
    ) -> None:
        """Store a range assembled from a cached overlap and a new tail.

        When ``destination_key`` is supplied, the assembled range is written
        under the new request's exact key and the source range remains intact.
        This is required for rolling windows whose start date changed.
        """
        storage_key = destination_key or original_key
        self.set(storage_key, all_bars, ttl=ttl, tag="price_history")
        self._record_range_index(
            self._range_index_key(symbol, interval),
            original_start,
            new_end,
            storage_key,
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
        today = date.today()
        entries = sorted(
            self._load_range_index(index_key),
            key=lambda entry: (entry.end, -entry.start.toordinal()),
            reverse=True,
        )
        for entry in entries:
            # A cached range [e_start, e_end] covers [start, end] when:
            #   - e_start <= start (covers the beginning)
            #   - either e_end >= end  (exact or superset)
            #     OR the gap (e_end, end] contains no trading days AND
            #        end <= today (future ranges are not pre-coverable)
            if entry.start > start:
                continue
            if entry.end < end:
                if end > today:
                    continue  # cannot cover future trading days
                if _has_trading_days_in_gap(entry.end, end):
                    continue  # genuine gap — new bars may exist
            cached = self.get(entry.key)
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
        new_entry = _RangeIndexEntry(start=start, end=end, key=key)
        with self._cache.transact():
            entries = [entry for entry in self._load_range_index(index_key) if entry.key != key]
            entries.append(new_entry)
            if len(entries) > max_entries:
                entries = entries[-max_entries:]
            self._cache.set(
                index_key,
                json.dumps([entry.to_raw() for entry in entries]),
                expire=ttl,
            )

    def _load_range_index(self, index_key: str) -> list[_RangeIndexEntry]:
        raw = self._cache.get(index_key)
        if not raw:
            return []
        try:
            entries = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(entries, list):
            return []
        return [
            entry
            for entry in (_RangeIndexEntry.from_raw(item) for item in entries)
            if entry is not None
        ]


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
