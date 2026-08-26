"""OneFinanceClient — the public API.

Presents a single uniform interface across providers with transparent
caching and config-driven tier-walking via the ``ProviderRouter``.

See design doc §5 (architecture) and §11 (public API).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

if TYPE_CHECKING:
    from onefinance.indicators.core import TechnicalIndicators
    from onefinance.options.core import GEXSnapshot, MaxPainResult

from onefinance.audit._recorder import AuditContext, AuditRecorder
from onefinance.audit.log import AuditLog
from onefinance.audit.models import AuditStats
from onefinance.cache.keys import make_key
from onefinance.cache.manager import (
    CacheManager,
    default_ttl,
    ttl_for_option_chain,
    ttl_for_price_history,
    ttl_for_quote,
)
from onefinance.core._cached_dispatch import CachedDispatcher
from onefinance.core.config import OneFinanceConfig, load_config
from onefinance.core.errors import (
    AllProvidersFailedError,
    FinanceError,
    InvalidArgumentError,
    NotSupportedError,
)
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    DCFValuation,
    EarningsCalendarEntry,
    EarningsRecord,
    EconomicEvent,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    MarketSentiment,
    NewsArticle,
    OptionChain,
    OptionsAnalytics,
    PeerCompany,
    PriceBar,
    Quote,
    ScreenerResult,
    SectorInfo,
    ShortInterest,
    TreasuryRate,
)
from onefinance.core.router import ProviderRouter
from onefinance.options.core import IVRankResult
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")
B = TypeVar("B")

# Always fetch this many news articles and cache the full set; callers slice to their limit.
# Prevents limit=10 and limit=20 from producing separate cache keys for identical data.
_NEWS_FETCH_MAX = 50


@dataclass(frozen=True, slots=True)
class _DateKeyedCacheKeys:
    cache_key: str
    lkg_key: str


def _date_keyed_cache_keys(data_type: str, **params: Any) -> _DateKeyedCacheKeys:
    """Build today's cache key plus the date-free last-known-good key."""
    return _DateKeyedCacheKeys(
        cache_key=make_key(data_type, **params, date=date.today()),
        lkg_key=make_key(data_type, **params),
    )


class OneFinanceClient:
    """Unified financial data client with transparent caching.

    Supports two construction modes:

    1.  **Explicit providers** — pass a list of ``BaseProvider`` instances
        and optionally a config for tier tables / cooldowns.
    2.  **Config-only** — pass a YAML config path; providers are
        instantiated from config (future: auto-discovery).

    Parameters
    ----------
    providers:
        One or more provider instances.  When combined with *config*,
        the router uses the config's tier tables to order them.
    config:
        Path to a YAML config file, or a pre-built ``OneFinanceConfig``.
        When ``None``, sensible defaults are used.
    cache_dir:
        Directory for the diskcache store.  Overrides config.
    cache_size_limit_gb:
        Max cache size in GB (LRU eviction when exceeded).
    """

    def __init__(
        self,
        providers: list[BaseProvider] | BaseProvider | None = None,
        *,
        config: str | Path | OneFinanceConfig | None = None,
        cache_dir: str | Path | None = None,
        cache_size_limit_gb: float | None = None,
        audit: bool = True,
        audit_log_path: str | Path | None = None,
        audit_retention_days: int = 30,
        fallback_order: list[str] | None = None,
    ) -> None:
        # Load config
        if isinstance(config, OneFinanceConfig):
            self._config = config
        else:
            self._config = load_config(config)

        # Per-call override trumps config fallback_order.
        if fallback_order is not None:
            self._config.fallback_order = fallback_order

        # Normalise providers to a dict keyed by name
        if providers is None:
            providers = _providers_from_config(self._config)
        elif isinstance(providers, BaseProvider):
            providers = [providers]
        self._provider_list: list[BaseProvider] = providers
        self._provider_map: dict[str, BaseProvider] = {p.name: p for p in providers}

        self._audit = AuditLog(
            audit_log_path,
            retention_days=audit_retention_days,
            enabled=audit,
        )
        self._audit_recorder = AuditRecorder(self._audit)

        # ProviderRouter and CacheManager
        cache_dir_path = Path(cache_dir) if cache_dir else Path(self._config.cache.dir).expanduser()
        cache_gb = (
            cache_size_limit_gb
            if cache_size_limit_gb is not None
            else self._config.cache.size_limit_gb
        )
        self._cache = CacheManager(
            cache_dir=cache_dir_path,
            size_limit_gb=cache_gb,
        )

        self._router = ProviderRouter(
            self._provider_map,
            self._config,
            audit_log=self._audit,
            cache=self._cache,
        )

        self._cached_dispatcher = CachedDispatcher(
            cache=self._cache,
            router=self._router,
            audit=self._audit_recorder,
            stale=self._config.stale,
            ttl_overrides=self._config.cache.ttl_overrides,
        )
        self._fetch_locks = self._cached_dispatcher.locks

    def close(self) -> None:
        """Release resources (closes cache and audit log)."""
        self._cache.close()
        self._audit.close()

    # -------------------------------------------------------------------
    # Audit log access
    # -------------------------------------------------------------------

    @property
    def audit_log(self) -> AuditLog:
        """Access the audit log for querying and inspection."""
        return self._audit

    def audit_stats(self, *, since: datetime | None = None) -> AuditStats:
        """Return aggregate audit statistics.

        Parameters
        ----------
        since:
            Start of the stats period.  Defaults to 24 hours ago.
        """
        return self._audit.stats(since=since)

    # -------------------------------------------------------------------
    # Provider health
    # -------------------------------------------------------------------

    def check_providers(
        self,
        *,
        ping: bool = False,
        ping_symbol: str = "AAPL",
        ping_timeout_s: float = 5.0,
        only: str | None = None,
    ) -> dict[str, Any]:
        """Run config + optional API health checks across providers.

        Returns a structured report (see :mod:`onefinance.core.health`).
        Always succeeds — every failure is encoded in the report rather
        than raised.
        """
        from onefinance.core.health import check_providers_health

        try:
            plan_gated = self._cache.list_global_negatives()
        except Exception:
            plan_gated = []

        return check_providers_health(
            self._config,
            self._provider_map,
            ping=ping,
            ping_symbol=ping_symbol,
            ping_timeout_s=ping_timeout_s,
            only=only,
            plan_gated=plan_gated,
        )

    def doctor(self, *, config_path: str | None = None) -> dict[str, Any]:
        """Run the config doctor — checks for common setup issues and suggests fixes.

        Returns a structured report with severity-tagged findings.  Always
        succeeds — every issue is encoded in the report rather than raised.
        """
        from onefinance.core.doctor import run_doctor

        return run_doctor(
            self._config,
            self._provider_map,
            config_path=config_path,
        )

    def __enter__(self) -> OneFinanceClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -------------------------------------------------------------------
    # Public accessors
    # -------------------------------------------------------------------

    @property
    def cache(self) -> CacheManager:
        """Access the cache manager for stats and invalidation."""
        return self._cache

    @property
    def config(self) -> OneFinanceConfig:
        """Access the resolved client configuration."""
        return self._config

    @property
    def providers(self) -> ProviderRouter:
        """Access the provider router for state inspection."""
        return self._router

    def batch(
        self,
        func: Callable[..., R],
        symbols: Sequence[str],
        *args: Any,
        max_workers: int = 8,
        **kwargs: Any,
    ) -> dict[str, R]:
        """Execute *func(symbol, *args, **kwargs)* concurrently across symbols.

        Parameters
        ----------
        func:
            Client method to call for each symbol (e.g. ``client.get_quote``).
        symbols:
            List of ticker symbols.
        *args:
            Positional arguments forwarded to *func* after symbol.
        max_workers:
            Maximum thread pool concurrency.
        **kwargs:
            Keyword arguments forwarded to *func*.

        Returns
        -------
        dict[str, R]
            Mapping of symbol to the returned result.
        """
        import concurrent.futures

        if not symbols:
            return {}

        results: dict[str, R] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(max_workers, len(symbols))
        ) as executor:
            futures = {executor.submit(func, sym, *args, **kwargs): sym for sym in symbols}
            for future in concurrent.futures.as_completed(futures):
                sym = futures[future]
                results[sym] = future.result()
        return results

    # -------------------------------------------------------------------
    # Type A — historical, no freshness argument
    # -------------------------------------------------------------------

    def get_price_history(
        self,
        symbol: str,
        start: date | str,
        end: date | str | None = None,
        interval: str = "1d",
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[PriceBar]:
        """Fetch OHLCV bars for *symbol* between *start* and *end*.

        Type A endpoint — TTL is auto-derived from the date range.
        """
        start_d = _parse_date(start)
        end_d = _parse_date(end) if end else date.today()

        if start_d > end_d:
            raise InvalidArgumentError(f"start ({start_d}) must be <= end ({end_d})")

        sym = symbol.upper()
        cache_key = make_key(
            "price_history",
            symbol=sym,
            start=start_d,
            end=end_d,
            interval=interval,
        )
        effective_ttl = ttl if ttl is not None else ttl_for_price_history(start_d, end_d)

        # Range subsumption (serve subranges from a cached superset) is only
        # safe for daily bars, which are settled and complete. Intraday
        # providers cap responses to the most recent N bars, so a cached
        # "wide" range may be missing older bars — slicing it would silently
        # return an incomplete answer. Restrict to interval == "1d".
        subsumable = interval == "1d"
        secondary_get = (
            (lambda: self._cache.find_covering_price_range(sym, interval, start_d, end_d))
            if subsumable
            else None
        )
        on_store = (
            (lambda _bars: self._cache.record_price_range(sym, interval, start_d, end_d, cache_key))
            if subsumable
            else None
        )

        # Resolve a partial overlap only after exact and covering cache lookups
        # miss, inside the dispatcher's per-key lock. This prevents an older
        # overlap from triggering a provider call when the exact assembled
        # range is already cached, and coalesces concurrent rolling windows.
        miss_resolver: Callable[[], list[PriceBar] | None] | None = None
        if subsumable:

            def resolve_overlap() -> list[PriceBar] | None:
                return self._try_extend_price_history(
                    symbol=sym,
                    start=start_d,
                    end=end_d,
                    interval=interval,
                    ttl=effective_ttl,
                    provider_name=provider,
                    cache_key=cache_key,
                    audit_context=AuditContext.new(
                        "price_history",
                        symbol=sym,
                        cache_key=cache_key,
                    ),
                )

            miss_resolver = resolve_overlap

        bars: list[PriceBar] = self._cached_fetch(
            cache_key=cache_key,
            endpoint="price_history",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=sym,
            fetch_fn=lambda p: p.get_price_history(sym, start_d, end_d, interval),
            secondary_get=secondary_get,
            miss_resolver=miss_resolver,
            on_store=on_store,
        )
        # Enforce [start, end] boundary regardless of provider or cache source.
        # Prevents out-of-range bars from causing date misalignment across symbols.
        return [b for b in bars if start_d <= b.date <= end_d]

    def _try_extend_price_history(
        self,
        *,
        symbol: str,
        start: date,
        end: date,
        interval: str,
        ttl: int,
        provider_name: str | None,
        cache_key: str,
        audit_context: AuditContext,
    ) -> list[PriceBar] | None:
        """Extend an existing daily price range by fetching only the missing tail."""
        delta = self._cache.find_extendable_price_range(symbol, interval, start, end)
        if delta is None:
            return None

        cached_bars, cached_end, cached_key = delta
        tail_start = cached_end + timedelta(days=1)
        try:
            new_bars: list[PriceBar] = self._router.dispatch(
                "price_history",
                lambda p: p.get_price_history(symbol, tail_start, end, interval),
                provider_name=provider_name,
                context=audit_context,
            )
            existing_dates = {b.date for b in cached_bars}
            merged = list(cached_bars) + [b for b in new_bars if b.date not in existing_dates]
            merged.sort(key=lambda b: b.date)
            self._cache.extend_price_range(
                symbol,
                interval,
                original_start=start,
                original_end=cached_end,
                new_end=end,
                original_key=cached_key,
                all_bars=merged,
                ttl=ttl,
                destination_key=cache_key,
            )
            logger.debug(
                "Delta-fetch %s: +%d bars (had %d, total %d)",
                symbol,
                len(new_bars),
                len(cached_bars),
                len(merged),
            )
            return [b for b in merged if start <= b.date <= end]
        except Exception:
            logger.debug("Delta-fetch failed for %s, falling through to full fetch", symbol)
            return None

    def get_info(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> CompanyInfo:
        """Fetch company profile for *symbol*.

        Type A endpoint — cached for 30 days by default.
        """
        return _single(
            self._cached_symbol_fetch(
                symbol,
                endpoint="info",
                ttl=ttl,
                no_cache=no_cache,
                provider=provider,
                fetch_fn=lambda p, sym: p.get_info(sym),
            )
        )

    def get_infos(
        self,
        symbols: list[str],
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[CompanyInfo | FinanceError]:
        """Fetch company profiles for multiple *symbols*.

        Caching is handled on a per-symbol basis to maximize hit rates.
        """
        if not symbols:
            return []

        normalized = [s.upper() for s in symbols]
        effective_ttl = ttl if ttl is not None else self._default_ttl("info")

        return self._cached_batch_fetch(
            symbols=normalized,
            endpoint="infos",
            data_type="info",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p, missing: p.get_infos(missing),
        )

    def get_financials(
        self,
        symbol: str,
        statement: str = "income",
        period: str = "annual",
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[IncomeStatement | BalanceSheet | CashFlow]:
        """Fetch financial statements for *symbol*.

        Type A endpoint — cached for 7 days by default.
        """
        return self._cached_date_symbol_fetch(
            symbol,
            endpoint="financials",
            key_params={"statement": statement, "period": period},
            ttl=ttl,
            no_cache=no_cache,
            provider=provider,
            fetch_fn=lambda p, sym: p.get_financials(sym, statement, period),
        )

    def get_insider_trades(
        self,
        symbol: str,
        since: date | str | None = None,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[InsiderTrade]:
        """Fetch insider trades for *symbol*.

        Type A endpoint — cached for 1 day by default.
        """
        since_d = _parse_date(since) if since else None
        sym = symbol.upper()
        cache_key = make_key("insider_trades", symbol=sym, since=since_d)

        # If caller requests a since-filtered view, try slicing a cached full result
        # (since=None key) before hitting the provider.
        secondary_get = None
        if since_d is not None:
            null_key = make_key("insider_trades", symbol=sym)

            def secondary_get() -> list[Any] | None:
                return _slice_insider_trades(self._cache.get(null_key), since_d)

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="insider_trades",
            ttl=ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=sym,
            fetch_fn=lambda p: p.get_insider_trades(sym, since_d),
            secondary_get=secondary_get,
        )

    # -------------------------------------------------------------------
    # Type B — always current
    # -------------------------------------------------------------------

    def get_quote(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
        enrich: bool = True,
    ) -> Quote:
        """Fetch current quote for *symbol*.

        Type B endpoint — market-aware TTL: 30s open, 2 min closed, 30 min weekend.

        Set ``enrich=False`` to return the primary provider's fresh quote without
        null-fill enrichment (currently volume for Finnhub).  Lightweight and
        enriched results intentionally use separate cache keys so an opt-out
        never makes a later enriched call look complete.
        """
        effective_ttl = ttl if ttl is not None else ttl_for_quote()

        return _single(
            self._cached_symbol_fetch(
                symbol,
                endpoint="quote",
                ttl=effective_ttl,
                no_cache=no_cache,
                provider=provider,
                data_type="quote" if enrich else "quote_unenriched",
                augment=enrich,
                fetch_fn=lambda p, sym: _fetch_validated_quote(p, sym),
            )
        )

    def get_quotes(
        self,
        symbols: list[str],
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[Quote | FinanceError]:
        """Fetch current quotes for multiple *symbols*.

        Uses native batching when the provider supports it, falling back to
        concurrent requests internally. Caching is handled on a per-symbol
        basis to maximize hit rates.
        """
        if not symbols:
            return []

        effective_ttl = ttl if ttl is not None else ttl_for_quote()
        normalized = [s.upper() for s in symbols]

        return self._cached_batch_fetch(
            symbols=normalized,
            endpoint="quotes",
            data_type="quote",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p, missing: _drop_invalid_quotes(p.get_quotes(missing)),
        )

    # -------------------------------------------------------------------
    # Type C — caller decides via fresh=
    # -------------------------------------------------------------------

    def get_ratios(
        self,
        symbol: str,
        period: str = "annual",
        *,
        fresh: bool = False,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[FinancialRatios]:
        """Fetch financial ratios for *symbol*.

        Type C endpoint — ``fresh=True`` uses short TTL and
        premium-first provider order.
        """
        return self._cached_date_symbol_fetch(
            symbol,
            endpoint="ratios",
            key_params={"period": period},
            ttl=ttl,
            no_cache=no_cache,
            provider=provider,
            fresh=fresh,
            fetch_fn=lambda p, sym: p.get_ratios(sym, period),
        )

    def get_earnings(
        self,
        symbol: str,
        *,
        fresh: bool = False,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[EarningsRecord]:
        """Fetch earnings records for *symbol*.

        Type C endpoint — ``fresh=True`` uses short TTL.
        """
        return self._cached_date_symbol_fetch(
            symbol,
            endpoint="earnings",
            ttl=ttl,
            no_cache=no_cache,
            provider=provider,
            fresh=fresh,
            fetch_fn=lambda p, sym: p.get_earnings(sym),
        )

    # -------------------------------------------------------------------
    # DCF Valuation — Type A
    # -------------------------------------------------------------------

    def get_dcf(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> DCFValuation:
        """Fetch DCF valuation for *symbol*.

        Type A endpoint — cached for 7 days by default.
        """
        return _single(
            self._cached_symbol_fetch(
                symbol,
                endpoint="dcf",
                ttl=ttl,
                no_cache=no_cache,
                provider=provider,
                fetch_fn=lambda p, sym: p.get_dcf(sym),
            )
        )

    # -------------------------------------------------------------------
    # Derived — technical indicators computed from price_history
    # -------------------------------------------------------------------

    def get_indicators(
        self,
        symbol: str,
        start: date | str | None = None,
        end: date | str | None = None,
        interval: str = "1d",
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
        with_quote: bool = True,
    ) -> TechnicalIndicators:
        """Compute a technical-indicator snapshot for *symbol*.

        Bars are fetched via ``get_price_history`` (so caching is shared
        with the ``price`` endpoint).  When *start* is omitted, defaults
        to the last 180 days, which is enough to populate MA60, MACD(26),
        and stable Wilder smoothing for RSI/ATR.

        Values are always computed from completed daily bars (see the
        ``TechnicalIndicators`` freshness contract). With ``with_quote=True``
        (default) the current quote is fetched — cheap, 30s-cached — and
        passed as the reference price, populating the live-classified
        ``support_levels_current``/``resistance_levels_current`` fields and
        the ``indicator_stale`` flag. Quote failure degrades gracefully to
        the bar-close-only view rather than failing the whole call.

        Raises
        ------
        ValueError
            If fewer than 5 bars are available (from ``compute_indicators``).
        """
        from onefinance.indicators.core import compute_indicators

        end_d = _parse_date(end) if end else date.today()
        start_d = _parse_date(start) if start else end_d - timedelta(days=180)

        bars = self.get_price_history(
            symbol,
            start=start_d,
            end=end_d,
            interval=interval,
            no_cache=no_cache,
            provider=provider,
            ttl=ttl,
        )

        reference_price: float | None = None
        reference_time: datetime | None = None
        if with_quote and bars:  # no bars → compute_indicators raises; skip the quote
            try:
                q = self.get_quote(symbol, no_cache=no_cache)
                reference_price = q.price
                reference_time = q.timestamp or q.fetched_at
            except FinanceError:
                logger.debug(
                    "Quote unavailable for %s — indicators served without live reference",
                    symbol,
                    exc_info=True,
                )

        return compute_indicators(
            bars,
            reference_price=reference_price,
            reference_time=reference_time,
        )

    # -------------------------------------------------------------------
    # Alternative Data Endpoints — Type A
    # -------------------------------------------------------------------

    def get_news(
        self,
        symbol: str,
        limit: int = 20,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[NewsArticle]:
        """Fetch recent news articles for *symbol*."""
        articles: list[NewsArticle] = self._cached_symbol_fetch(
            symbol,
            endpoint="news",
            ttl=ttl,
            no_cache=no_cache,
            provider=provider,
            fetch_fn=lambda p, sym: p.get_news(sym, limit=_NEWS_FETCH_MAX),
        )
        return articles if len(articles) <= limit else articles[:limit]

    def get_corporate_actions(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[CorporateAction]:
        """Fetch dividend and split history for *symbol*."""
        return self._cached_symbol_fetch(
            symbol,
            endpoint="corporate_actions",
            ttl=ttl,
            no_cache=no_cache,
            provider=provider,
            fetch_fn=lambda p, sym: p.get_corporate_actions(sym),
        )

    def get_institutional_holders(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[InstitutionalHolder]:
        """Fetch institutional holders for *symbol*."""
        return self._cached_symbol_fetch(
            symbol,
            endpoint="institutional_holders",
            ttl=ttl,
            no_cache=no_cache,
            provider=provider,
            fetch_fn=lambda p, sym: p.get_institutional_holders(sym),
        )

    def get_analyst_data(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> AnalystData:
        """Fetch analyst price targets and ratings for *symbol*."""
        return _single(
            self._cached_symbol_fetch(
                symbol,
                endpoint="analyst_data",
                ttl=ttl,
                no_cache=no_cache,
                provider=provider,
                fetch_fn=lambda p, sym: p.get_analyst_data(sym),
            )
        )

    def get_peers(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[PeerCompany]:
        """Fetch peer/comparable companies for *symbol*."""
        return self._cached_symbol_fetch(
            symbol,
            endpoint="peers",
            ttl=ttl,
            no_cache=no_cache,
            provider=provider,
            fetch_fn=lambda p, sym: p.get_peers(sym),
        )

    def get_options_expirations(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[date]:
        """Fetch available option expiration dates for *symbol*."""
        return self._cached_symbol_fetch(
            symbol,
            endpoint="options_expirations",
            ttl=ttl,
            no_cache=no_cache,
            provider=provider,
            fetch_fn=lambda p, sym: p.get_options_expirations(sym),
        )

    def get_option_chain(
        self,
        symbol: str,
        expiration: date,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> OptionChain:
        """Fetch the option chain for *symbol* and *expiration*."""
        sym = symbol.upper()
        cache_key = make_key("option_chain", symbol=sym, expiration=expiration)
        effective_ttl = ttl if ttl is not None else ttl_for_option_chain()

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="option_chain",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=sym,
            fetch_fn=lambda p: p.get_option_chain(sym, expiration),
        )

    def get_options_analytics(
        self,
        symbol: str,
        max_expirations: int = 6,
        *,
        no_cache: bool = False,
        provider: str | None = None,
    ) -> OptionsAnalytics:
        """Aggregate put/call ratio and open interest across option expirations.

        Fetches the nearest *max_expirations* option chains concurrently and
        aggregates puts and calls volume and open interest.  Relies on the
        per-chain cache (5-min TTL) rather than caching the derived result.
        """
        import concurrent.futures

        from onefinance.core.models import OptionsAnalytics
        from onefinance.options.core import assess_oi_reliability

        sym = symbol.upper()
        expirations = self.get_options_expirations(sym, no_cache=no_cache, provider=provider)
        selected = sorted(expirations)[:max_expirations]

        chains: list[OptionChain] = []
        failed_expirations: list[date] = []
        failures: list[tuple[str, FinanceError]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(selected), 6)) as executor:
            futures = {
                executor.submit(
                    self.get_option_chain, sym, exp, no_cache=no_cache, provider=provider
                ): exp
                for exp in selected
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    chains.append(future.result())
                except FinanceError as exc:
                    failed_expirations.append(futures[future])
                    failures.append(("option_chain", exc))
                except Exception as exc:  # noqa: BLE001 - preserve partial analytics context
                    failed_expirations.append(futures[future])
                    failures.append(
                        (
                            "option_chain",
                            FinanceError(
                                code="UNEXPECTED",
                                message=f"{type(exc).__name__}: {exc}",
                            ),
                        )
                    )

        if not chains:
            raise AllProvidersFailedError("option_chain", failures)

        total_call_vol = sum(c.volume or 0 for ch in chains for c in ch.calls)
        total_put_vol = sum(c.volume or 0 for ch in chains for c in ch.puts)
        total_call_oi = sum(c.open_interest or 0 for ch in chains for c in ch.calls)
        total_put_oi = sum(c.open_interest or 0 for ch in chains for c in ch.puts)

        # Providers (notably yfinance) intermittently zero out OI while volume
        # stays huge; a pcr_oi computed from a handful of surviving contracts
        # looks normal but is garbage. Null it out and warn instead.
        oi_reliable, oi_warning = assess_oi_reliability(chains)

        pcr_volume = round(total_put_vol / total_call_vol, 4) if total_call_vol > 0 else None
        pcr_oi = (
            round(total_put_oi / total_call_oi, 4) if total_call_oi > 0 and oi_reliable else None
        )

        source = chains[0].source if chains else "unknown"
        coverage_warning = (
            f"Used {len(chains)} of {len(selected)} requested expirations; "
            f"{len(failed_expirations)} chain fetches failed."
            if failed_expirations
            else None
        )

        return OptionsAnalytics(
            symbol=sym,
            pcr_volume=pcr_volume,
            pcr_oi=pcr_oi,
            total_put_volume=total_put_vol,
            total_call_volume=total_call_vol,
            total_put_oi=total_put_oi,
            total_call_oi=total_call_oi,
            oi_reliable=oi_reliable,
            oi_warning=oi_warning,
            expirations_requested=len(selected),
            expirations_used=len(chains),
            expirations_failed=sorted(failed_expirations),
            coverage_warning=coverage_warning,
            source=source,
            fetched_at=datetime.now(UTC),
        )

    def get_gex(
        self,
        symbol: str,
        max_expirations: int = 6,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        allow_black_scholes_gamma: bool = True,
        risk_free_rate: float | None = None,
    ) -> GEXSnapshot:
        """Compute dealer gamma-exposure (GEX) profile for *symbol*.

        Derived analytics — like ``get_indicators``, computed on the fly from
        already-cached (or freshly-fetched) option chains rather than cached
        itself.

        Providers without greeks (yfinance) still carry per-contract implied
        volatility, so by default missing gamma is backfilled with a
        Black-Scholes estimate (``options.core.synthesize_missing_gamma``)
        before aggregating — this is what makes GEX usable without a
        greeks-capable provider like Massive. Set
        ``allow_black_scholes_gamma=False`` to require real provider gamma
        instead (raises ``ValueError`` if none of the fetched chains carry
        it). ``GEXSnapshot.gamma_source`` reports which path was used.
        """
        import concurrent.futures

        from onefinance.options.core import compute_gex, synthesize_missing_gamma

        sym = symbol.upper()
        expirations = self.get_options_expirations(sym, no_cache=no_cache, provider=provider)
        selected = sorted(expirations)[:max_expirations]

        quote = self.get_quote(sym, no_cache=no_cache, provider=provider)

        chains: list[OptionChain] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(selected), 6)) as executor:
            futures = {
                executor.submit(
                    self.get_option_chain, sym, exp, no_cache=no_cache, provider=provider
                ): exp
                for exp in selected
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    chains.append(future.result())
                except Exception:
                    pass

        gamma_source: Literal["provider", "black_scholes", "mixed"] = "provider"
        if allow_black_scholes_gamma and chains:
            had_provider_gamma = any(
                c.gamma is not None for ch in chains for c in (*ch.calls, *ch.puts)
            )
            rf_kwargs = {} if risk_free_rate is None else {"risk_free_rate": risk_free_rate}
            today = datetime.now(UTC).date()
            synthesized_any = False
            new_chains = []
            for ch in chains:
                new_ch, filled = synthesize_missing_gamma(ch, quote.price, as_of=today, **rf_kwargs)
                new_chains.append(new_ch)
                synthesized_any = synthesized_any or filled
            chains = new_chains
            if synthesized_any:
                gamma_source = "mixed" if had_provider_gamma else "black_scholes"

        source = chains[0].source if chains else quote.source
        return compute_gex(
            chains,
            quote.price,
            sym,
            fetched_at=datetime.now(UTC),
            source=source,
            gamma_source=gamma_source,
            risk_free_rate=risk_free_rate,
        )

    def get_max_pain(
        self,
        symbol: str,
        expiration: date,
        *,
        no_cache: bool = False,
        provider: str | None = None,
    ) -> MaxPainResult:
        """Compute the max-pain strike for *symbol* at *expiration*.

        Derived analytics — computed from a single ``get_option_chain`` call,
        not cached separately.
        """
        from onefinance.options.core import compute_max_pain

        sym = symbol.upper()
        chain = self.get_option_chain(sym, expiration, no_cache=no_cache, provider=provider)
        return compute_max_pain(chain, fetched_at=datetime.now(UTC), source=chain.source)

    def get_iv_rank(
        self,
        symbol: str,
        *,
        expiration: date | None = None,
        lookback_days: int = 252,
        no_cache: bool = False,
        provider: str | None = None,
    ) -> IVRankResult:
        """Compute IV rank for *symbol*'s at-the-money implied volatility.

        Derived analytics like ``get_gex``/``get_max_pain``, but not purely
        point-in-time: no provider here supplies a historical IV series, so
        each call also records today's ATM IV observation (deduped by
        calendar day) into a persistent per-symbol cache history (see
        ``CacheManager.record_iv_observation``), and ranks today's reading
        against that accumulated history. ``iv_rank``/``iv_percentile`` stay
        ``None`` until enough distinct days have been observed — see
        ``IVRankResult.insufficient_history``.

        Defaults to the nearest expiration when *expiration* is omitted.
        """
        from onefinance.options.core import compute_atm_iv, compute_iv_rank

        sym = symbol.upper()
        if expiration is None:
            expirations = self.get_options_expirations(sym, no_cache=no_cache, provider=provider)
            if not expirations:
                raise ValueError(
                    f"No option expirations available for {sym} — cannot compute IV rank."
                )
            expiration = min(expirations)

        chain = self.get_option_chain(sym, expiration, no_cache=no_cache, provider=provider)
        quote = self.get_quote(sym, no_cache=no_cache, provider=provider)

        atm_iv = compute_atm_iv(chain, quote.price)
        if atm_iv is None:
            raise ValueError(
                f"No implied-volatility data in the {expiration} chain for {sym} — "
                "cannot compute IV rank."
            )

        today = date.today()
        self._cache.record_iv_observation(sym, today, atm_iv)
        history = self._cache.get_iv_history(sym, lookback_days)

        return compute_iv_rank(
            symbol=sym,
            expiration=expiration,
            atm_iv=atm_iv,
            history=history,
            lookback_days=lookback_days,
            source=chain.source,
            fetched_at=datetime.now(UTC),
        )

    def get_short_interest(
        self,
        symbol: str,
        *,
        ttl: int | None = None,
        no_cache: bool = False,
        provider: str | None = None,
    ) -> ShortInterest:
        """Fetch short interest and days-to-cover for *symbol*."""
        return _single(
            self._cached_symbol_fetch(
                symbol,
                endpoint="short_interest",
                ttl=ttl,
                no_cache=no_cache,
                provider=provider,
                fetch_fn=lambda p, sym: p.get_short_interest(sym),
            )
        )

    def get_market_sentiment(
        self,
        *,
        ttl: int | None = None,
        no_cache: bool = False,
        provider: str | None = None,
    ) -> MarketSentiment:
        """Fetch market-wide put/call ratio data."""
        cache_key = make_key("market_sentiment")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="market_sentiment",
            ttl=ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_market_sentiment(),
        )

    def screen_stocks(
        self,
        query: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[ScreenerResult]:
        """Screen stocks based on a provider-specific query string."""
        cache_key = make_key("screen_stocks", query=query.strip().lower())

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="screen_stocks",
            ttl=ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.screen_stocks(query),
        )

    def get_sector_overview(
        self,
        sector: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> SectorInfo:
        """Fetch overview for a specific sector."""
        cache_key = make_key("sector_overview", sector=sector.lower())

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="sector_overview",
            ttl=ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_sector_overview(sector),
        )

    def get_earnings_calendar(
        self,
        start: date | str | None = None,
        end: date | str | None = None,
        *,
        symbol: str | None = None,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[EarningsCalendarEntry]:
        """Fetch scheduled earnings releases for a date range.

        Type A endpoint — cached for 4 hours by default.

        Parameters
        ----------
        start:
            Start of the date range (inclusive).  Defaults to today.
        end:
            End of the date range (inclusive).  Defaults to today + 7 days.
        symbol:
            Optional ticker filter applied after fetching.
        """
        start_d = _parse_date(start) if start else date.today()
        end_d = _parse_date(end) if end else date.today() + timedelta(days=7)

        results: list[EarningsCalendarEntry] = self._cached_calendar_fetch(
            start=start_d,
            end=end_d,
            endpoint="earnings_calendar",
            date_field="report_date",
            ttl=ttl,
            no_cache=no_cache,
            provider=provider,
            fetch_fn=lambda p: p.get_earnings_calendar(start_d, end_d),
        )

        if symbol:
            sym_upper = symbol.upper()
            results = [e for e in results if e.symbol == sym_upper]

        return results

    def get_economic_calendar(
        self,
        start: date | str | None = None,
        end: date | str | None = None,
        *,
        country: str | None = None,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[EconomicEvent]:
        """Fetch macro economic events for a date range.

        Type A endpoint — cached 4 hours by default.

        Parameters
        ----------
        start:
            Start of range (inclusive). Defaults to today.
        end:
            End of range (inclusive). Defaults to today + 7 days.
        country:
            Optional ISO 3166-1 alpha-2 filter applied after fetching (e.g. "US").
        """
        start_d = _parse_date(start) if start else date.today()
        end_d = _parse_date(end) if end else date.today() + timedelta(days=7)

        results: list[EconomicEvent] = self._cached_calendar_fetch(
            start=start_d,
            end=end_d,
            endpoint="economic_calendar",
            date_field="event_date",
            ttl=ttl,
            no_cache=no_cache,
            provider=provider,
            fetch_fn=lambda p: p.get_economic_calendar(start_d, end_d),
        )

        if country:
            country_upper = country.upper()
            results = [e for e in results if (e.country or "").upper() == country_upper]

        return results

    def get_treasury_rates(
        self,
        start: date | str | None = None,
        end: date | str | None = None,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[TreasuryRate]:
        """Fetch US Treasury yield-curve observations for a date range.

        Type A endpoint — cached for 7 days by default.
        """
        start_d = _parse_date(start) if start else date.today() - timedelta(days=30)
        end_d = _parse_date(end) if end else date.today()

        cache_key = make_key("treasury_rates", start=start_d, end=end_d)

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="treasury_rates",
            ttl=ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_treasury_rates(start_d, end_d),
        )

    def get_forward_estimates(
        self,
        symbol: str,
        *,
        ttl: int | None = None,
        no_cache: bool = False,
        provider: str | None = None,
    ) -> list[ForwardEstimates]:
        """Fetch consensus forward-looking estimates for *symbol*."""
        return self._cached_date_symbol_fetch(
            symbol,
            data_type="estimates",
            endpoint="forward_estimates",
            ttl=ttl,
            no_cache=no_cache,
            provider=provider,
            fetch_fn=lambda p, sym: p.get_forward_estimates(sym),
        )

    # -------------------------------------------------------------------
    # Internal: TTL resolution
    # -------------------------------------------------------------------

    def _cached_symbol_fetch(
        self,
        symbol: str,
        *,
        endpoint: str,
        fetch_fn: Callable[[BaseProvider, str], R],
        ttl: int | None,
        no_cache: bool,
        provider: str | None,
        data_type: str | None = None,
        augment: bool | None = None,
    ) -> R:
        """Run the common provider-agnostic cache path for a symbol endpoint."""
        normalized = symbol.upper()
        return self._cached_fetch(
            cache_key=make_key(data_type or endpoint, symbol=normalized),
            endpoint=endpoint,
            ttl=ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=normalized,
            fetch_fn=lambda current_provider: fetch_fn(current_provider, normalized),
            augment=augment,
        )

    def _cached_date_symbol_fetch(
        self,
        symbol: str,
        *,
        endpoint: str,
        fetch_fn: Callable[[BaseProvider, str], R],
        ttl: int | None,
        no_cache: bool,
        provider: str | None,
        fresh: bool = False,
        data_type: str | None = None,
        key_params: dict[str, Any] | None = None,
    ) -> R:
        """Fetch a daily-keyed symbol result with a date-free stale key."""
        normalized = symbol.upper()
        keys = _date_keyed_cache_keys(
            data_type or endpoint,
            symbol=normalized,
            **(key_params or {}),
        )
        return self._cached_fetch(
            cache_key=keys.cache_key,
            endpoint=endpoint,
            ttl=ttl,
            no_cache=no_cache,
            provider_name=provider,
            fresh=fresh,
            symbol=normalized,
            fetch_fn=lambda current_provider: fetch_fn(current_provider, normalized),
            lkg_key=keys.lkg_key,
        )

    def _cached_calendar_fetch(
        self,
        *,
        start: date,
        end: date,
        endpoint: str,
        date_field: str,
        fetch_fn: Callable[[BaseProvider], R],
        ttl: int | None,
        no_cache: bool,
        provider: str | None,
    ) -> R:
        """Fetch a calendar range with covering-range cache reuse."""
        cache_key = make_key(endpoint, start=start, end=end)
        return self._cached_fetch(
            cache_key=cache_key,
            endpoint=endpoint,
            ttl=ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=fetch_fn,
            secondary_get=lambda: cast(
                R | None,
                self._cache.find_covering_calendar_range(endpoint, start, end, date_field),
            ),
            on_store=lambda _: self._cache.record_calendar_range(endpoint, start, end, cache_key),
        )

    def _default_ttl(self, endpoint: str, *, fresh: bool = False) -> int:
        """Resolve the effective TTL for *endpoint* using config overrides."""
        return default_ttl(
            endpoint,
            fresh=fresh,
            overrides=self._config.cache.ttl_overrides,
        )

    # -------------------------------------------------------------------
    # Internal: cache → router dispatch
    # -------------------------------------------------------------------

    def _cached_fetch(
        self,
        *,
        cache_key: str,
        endpoint: str,
        ttl: int | None = None,
        no_cache: bool,
        provider_name: str | None,
        fetch_fn: Callable[[BaseProvider], T],
        augment: bool | None = None,
        fresh: bool = False,
        symbol: str | None = None,
        secondary_get: Callable[[], T | None] | None = None,
        miss_resolver: Callable[[], T | None] | None = None,
        on_store: Callable[[T], None] | None = None,
        lkg_key: str | None = None,
    ) -> T:
        """Check cache, then dispatch via the provider router.

        The router handles tier walking, cooldown management, and
        fallback logic.

        ``secondary_get`` is an optional fallback lookup tried on an exact
        cache miss (e.g. serving a sub-range from a cached superset). When
        it returns a value, that value is used without a provider call.
        ``on_store`` runs after a fresh result is cached, letting callers
        register auxiliary indexes (e.g. the price-range index).

        ``lkg_key`` is the key under which the long-lived last-known-good
        copy is stored/read for stale-on-error. It defaults to
        ``cache_key``, but date-keyed endpoints (whose ``cache_key`` rolls
        over daily) must pass a *date-free* stable key so the LKG copy
        survives across calendar days — otherwise the stale fallback could
        only ever hit within the same day the copy was written.
        """
        return self._cached_dispatcher.fetch(
            cache_key=cache_key,
            endpoint=endpoint,
            ttl=ttl,
            no_cache=no_cache,
            provider_name=provider_name,
            fetch_fn=fetch_fn,
            augment=augment,
            fresh=fresh,
            symbol=symbol,
            secondary_get=secondary_get,
            miss_resolver=miss_resolver,
            on_store=on_store,
            lkg_key=lkg_key,
        )

    def _cached_batch_fetch(
        self,
        *,
        symbols: list[str],
        endpoint: str,
        data_type: str,
        ttl: int,
        no_cache: bool,
        provider_name: str | None,
        fetch_fn: Callable[[BaseProvider, list[str]], list[B]],
    ) -> list[B | FinanceError]:
        """Check cache per symbol, dispatch misses as a batch, cache responses per symbol.

        Returns results in the exact order requested. If the batch fetch fails,
        returns the exception in place of the results for the missing symbols.
        """
        return self._cached_dispatcher.fetch_batch(
            symbols=symbols,
            endpoint=endpoint,
            data_type=data_type,
            ttl=ttl,
            no_cache=no_cache,
            provider_name=provider_name,
            fetch_fn=fetch_fn,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_validated_quote(provider: BaseProvider, symbol: str) -> Quote:
    """Fetch a quote and reject a garbage ``price == 0`` result.

    Some providers return HTTP 200 with a zero price for symbols they don't
    genuinely carry (e.g. certain volatility indices on Finnhub's free tier)
    instead of a clean error. Raising ``NotSupportedError`` here reuses the
    router's existing per-symbol negative-cache and tier-fallback machinery —
    the next provider in the tier gets a chance, and this (provider, symbol)
    pair is skipped without an HTTP call for 24h, rather than the client
    silently returning `price: 0` as if it were a real quote.
    """
    q = provider.get_quote(symbol)
    if q.price <= 0:
        raise NotSupportedError(provider.name, "quote")
    return q


def _drop_invalid_quotes(quotes: list[Quote]) -> list[Quote]:
    """Filter zero-price quotes out of a batch result before it's cached.

    A dropped symbol falls through ``_cached_batch_fetch``'s existing
    short-batch handling and comes back as a ``BATCH_RESULT_MISSING`` error
    for that symbol, rather than silently caching a garbage ``price: 0``
    quote for the batch's 30s-ish TTL.
    """
    return [q for q in quotes if q.price > 0]


def _single(result: T | list[T]) -> T:
    """Coerce a provider result to a single model.

    Endpoints that logically return one model still receive a list from
    providers that only implement a list-returning shape; unwrap the first
    element in that case, otherwise pass the model through unchanged.
    """
    if isinstance(result, list):
        if not result:
            raise FinanceError(
                "EMPTY_PROVIDER_RESULT",
                "Provider returned an empty result for a single-model endpoint",
            )
        return result[0]
    return result


def _slice_insider_trades(cached: Any, since: date) -> list[Any] | None:
    """Filter a cached full insider-trades list to trades on or after *since*.

    Returns None if nothing is cached (caller must fetch), or an empty list
    if cached but no trades match — both are valid and distinct outcomes.
    """
    if not isinstance(cached, list):
        return None
    return [t for t in cached if (t.trade_date or t.filing_date) >= since]


def _parse_date(value: date | str | None) -> date:
    """Coerce a string or date to a ``date`` object."""
    if value is None:
        return date.today()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _providers_from_config(config: OneFinanceConfig) -> list[BaseProvider]:
    """Instantiate all providers whose credentials are available.

    Delegates to :func:`onefinance.providers._factory.build_all`. The
    ``providers`` subpackage import triggers spec registration as a side
    effect, so the registry is always populated before the first call.
    """
    import onefinance.providers  # noqa: F401  — populate registry
    from onefinance.providers._factory import build_all

    return build_all(config)
