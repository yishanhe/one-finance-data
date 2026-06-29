"""OneFinanceClient — the public API.

Presents a single uniform interface across providers with transparent
caching and config-driven tier-walking via the ``ProviderRouter``.

See design doc §5 (architecture) and §11 (public API).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:
    from onefinance.indicators.core import TechnicalIndicators

from onefinance.audit._recorder import AuditRecorder
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
from onefinance.core.config import OneFinanceConfig, load_config
from onefinance.core.errors import (
    AllProvidersFailedError,
    FinanceError,
    InvalidArgumentError,
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
)
from onefinance.core.router import ProviderRouter
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Always fetch this many news articles and cache the full set; callers slice to their limit.
# Prevents limit=10 and limit=20 from producing separate cache keys for identical data.
_NEWS_FETCH_MAX = 50


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

        # Initialise audit log + shared recorder
        self._audit = AuditLog(
            log_path=audit_log_path,
            retention_days=audit_retention_days,
            enabled=audit,
        )
        self._audit_recorder = AuditRecorder(self._audit)

        # Initialise cache (explicit args override config) — must be before router
        resolved_cache_dir = cache_dir or self._config.cache.dir
        resolved_size_limit = cache_size_limit_gb or self._config.cache.size_limit_gb
        self._cache = CacheManager(
            cache_dir=resolved_cache_dir,
            size_limit_gb=resolved_size_limit,
        )

        # Initialise the router (with audit log + cache for negative-caching)
        self._router = ProviderRouter(
            self._provider_map, self._config, audit_log=self._audit, cache=self._cache
        )

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

        return check_providers_health(
            self._config,
            self._provider_map,
            ping=ping,
            ping_symbol=ping_symbol,
            ping_timeout_s=ping_timeout_s,
            only=only,
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
    def providers(self) -> ProviderRouter:
        """Access the provider router for state inspection."""
        return self._router

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

        cache_key = make_key(
            "price_history",
            symbol=symbol.upper(),
            start=start_d,
            end=end_d,
            interval=interval,
        )
        effective_ttl = ttl if ttl is not None else ttl_for_price_history(start_d, end_d)
        sym = symbol.upper()

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

        bars: list[PriceBar] = self._cached_fetch(
            cache_key=cache_key,
            endpoint="price_history",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=sym,
            fetch_fn=lambda p: p.get_price_history(sym, start_d, end_d, interval),
            secondary_get=secondary_get,
            on_store=on_store,
        )
        # Enforce [start, end] boundary regardless of provider or cache source.
        # Prevents out-of-range bars from causing date misalignment across symbols.
        return [b for b in bars if start_d <= b.date <= end_d]

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
        cache_key = make_key("info", symbol=symbol.upper())
        effective_ttl = ttl if ttl is not None else self._default_ttl("info")

        result = self._cached_fetch(
            cache_key=cache_key,
            endpoint="info",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_info(symbol.upper()),
        )
        if isinstance(result, list):
            return result[0]
        return result

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
        lkg_key = make_key(
            "financials",
            symbol=symbol.upper(),
            statement=statement,
            period=period,
        )
        cache_key = make_key(
            "financials",
            symbol=symbol.upper(),
            statement=statement,
            period=period,
            date=date.today(),
        )
        effective_ttl = ttl if ttl is not None else self._default_ttl("financials")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="financials",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_financials(symbol.upper(), statement, period),
            lkg_key=lkg_key,
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
        effective_ttl = ttl if ttl is not None else self._default_ttl("insider_trades")

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
            ttl=effective_ttl,
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
    ) -> Quote:
        """Fetch current quote for *symbol*.

        Type B endpoint — market-aware TTL: 30s open, 2 min closed, 30 min weekend.
        """
        cache_key = make_key("quote", symbol=symbol.upper())
        effective_ttl = ttl if ttl is not None else ttl_for_quote()

        result = self._cached_fetch(
            cache_key=cache_key,
            endpoint="quote",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_quote(symbol.upper()),
        )
        if isinstance(result, list):
            return result[0]
        return result

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
            fetch_fn=lambda p, missing: p.get_quotes(missing),
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
        lkg_key = make_key(
            "ratios",
            symbol=symbol.upper(),
            period=period,
        )
        cache_key = make_key(
            "ratios",
            symbol=symbol.upper(),
            period=period,
            date=date.today(),
        )
        effective_ttl = ttl if ttl is not None else self._default_ttl("ratios", fresh=fresh)

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="ratios",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fresh=fresh,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_ratios(symbol.upper(), period),
            lkg_key=lkg_key,
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
        lkg_key = make_key(
            "earnings",
            symbol=symbol.upper(),
        )
        cache_key = make_key(
            "earnings",
            symbol=symbol.upper(),
            date=date.today(),
        )
        effective_ttl = ttl if ttl is not None else self._default_ttl("earnings", fresh=fresh)

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="earnings",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fresh=fresh,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_earnings(symbol.upper()),
            lkg_key=lkg_key,
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
        cache_key = make_key("dcf", symbol=symbol.upper())
        effective_ttl = ttl if ttl is not None else self._default_ttl("dcf")

        result = self._cached_fetch(
            cache_key=cache_key,
            endpoint="dcf",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_dcf(symbol.upper()),
        )
        if isinstance(result, list):
            return result[0]
        return result

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
    ) -> TechnicalIndicators:
        """Compute a technical-indicator snapshot for *symbol*.

        Bars are fetched via ``get_price_history`` (so caching is shared
        with the ``price`` endpoint).  When *start* is omitted, defaults
        to the last 180 days, which is enough to populate MA60, MACD(26),
        and stable Wilder smoothing for RSI/ATR.

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
        return compute_indicators(bars)

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
        sym = symbol.upper()
        cache_key = make_key("news", symbol=sym)
        effective_ttl = ttl if ttl is not None else self._default_ttl("news")

        articles: list[NewsArticle] = self._cached_fetch(
            cache_key=cache_key,
            endpoint="news",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=sym,
            fetch_fn=lambda p: p.get_news(sym, limit=_NEWS_FETCH_MAX),
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
        cache_key = make_key("corporate_actions", symbol=symbol.upper())
        effective_ttl = ttl if ttl is not None else self._default_ttl("corporate_actions")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="corporate_actions",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_corporate_actions(symbol.upper()),
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
        cache_key = make_key("institutional_holders", symbol=symbol.upper())
        effective_ttl = ttl if ttl is not None else self._default_ttl("institutional_holders")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="institutional_holders",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_institutional_holders(symbol.upper()),
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
        cache_key = make_key("analyst_data", symbol=symbol.upper())
        effective_ttl = ttl if ttl is not None else self._default_ttl("analyst_data")

        result = self._cached_fetch(
            cache_key=cache_key,
            endpoint="analyst_data",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_analyst_data(symbol.upper()),
        )
        if isinstance(result, list):
            return result[0]
        return result

    def get_peers(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[PeerCompany]:
        """Fetch peer/comparable companies for *symbol*."""
        cache_key = make_key("peers", symbol=symbol.upper())
        effective_ttl = ttl if ttl is not None else self._default_ttl("peers")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="peers",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_peers(symbol.upper()),
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
        cache_key = make_key("options_expirations", symbol=symbol.upper())
        effective_ttl = ttl if ttl is not None else self._default_ttl("options_expirations")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="options_expirations",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_options_expirations(symbol.upper()),
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
        cache_key = make_key("option_chain", symbol=symbol.upper(), expiration=expiration)
        effective_ttl = ttl if ttl is not None else ttl_for_option_chain()

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="option_chain",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_option_chain(symbol.upper(), expiration),
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

        sym = symbol.upper()
        expirations = self.get_options_expirations(sym, no_cache=no_cache, provider=provider)
        selected = sorted(expirations)[:max_expirations]

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

        total_call_vol = sum(c.volume or 0 for ch in chains for c in ch.calls)
        total_put_vol = sum(c.volume or 0 for ch in chains for c in ch.puts)
        total_call_oi = sum(c.open_interest or 0 for ch in chains for c in ch.calls)
        total_put_oi = sum(c.open_interest or 0 for ch in chains for c in ch.puts)

        pcr_volume = round(total_put_vol / total_call_vol, 4) if total_call_vol > 0 else None
        pcr_oi = round(total_put_oi / total_call_oi, 4) if total_call_oi > 0 else None

        source = chains[0].source if chains else "unknown"

        return OptionsAnalytics(
            symbol=sym,
            pcr_volume=pcr_volume,
            pcr_oi=pcr_oi,
            total_put_volume=total_put_vol,
            total_call_volume=total_call_vol,
            total_put_oi=total_put_oi,
            total_call_oi=total_call_oi,
            expirations_used=len(chains),
            source=source,
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
        cache_key = make_key("short_interest", symbol=symbol.upper())
        effective_ttl = ttl if ttl is not None else self._default_ttl("short_interest")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="short_interest",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_short_interest(symbol.upper()),
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
        effective_ttl = ttl if ttl is not None else self._default_ttl("market_sentiment")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="market_sentiment",
            ttl=effective_ttl,
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
        effective_ttl = ttl if ttl is not None else self._default_ttl("screen_stocks")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="screen_stocks",
            ttl=effective_ttl,
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
        effective_ttl = ttl if ttl is not None else self._default_ttl("sector_overview")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="sector_overview",
            ttl=effective_ttl,
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

        cache_key = make_key("earnings_calendar", start=start_d, end=end_d)
        effective_ttl = ttl if ttl is not None else self._default_ttl("earnings_calendar")

        results: list[EarningsCalendarEntry] = self._cached_fetch(
            cache_key=cache_key,
            endpoint="earnings_calendar",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_earnings_calendar(start_d, end_d),
            secondary_get=lambda: self._cache.find_covering_calendar_range(
                "earnings_calendar", start_d, end_d, "report_date"
            ),
            on_store=lambda _: self._cache.record_calendar_range(
                "earnings_calendar", start_d, end_d, cache_key
            ),
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

        cache_key = make_key("economic_calendar", start=start_d, end=end_d)
        effective_ttl = ttl if ttl is not None else self._default_ttl("economic_calendar")

        results: list[EconomicEvent] = self._cached_fetch(
            cache_key=cache_key,
            endpoint="economic_calendar",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_economic_calendar(start_d, end_d),
            secondary_get=lambda: self._cache.find_covering_calendar_range(
                "economic_calendar", start_d, end_d, "event_date"
            ),
            on_store=lambda _: self._cache.record_calendar_range(
                "economic_calendar", start_d, end_d, cache_key
            ),
        )

        if country:
            country_upper = country.upper()
            results = [e for e in results if (e.country or "").upper() == country_upper]

        return results

    def get_forward_estimates(
        self,
        symbol: str,
        *,
        ttl: int | None = None,
        no_cache: bool = False,
        provider: str | None = None,
    ) -> list[ForwardEstimates]:
        """Fetch consensus forward-looking estimates for *symbol*."""
        sym = symbol.upper()
        lkg_key = make_key("estimates", symbol=sym)
        cache_key = make_key("estimates", symbol=sym, date=date.today())
        effective_ttl = ttl if ttl is not None else self._default_ttl("forward_estimates")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="forward_estimates",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=sym,
            fetch_fn=lambda p: p.get_forward_estimates(sym),
            lkg_key=lkg_key,
        )

    # -------------------------------------------------------------------
    # Internal: TTL resolution
    # -------------------------------------------------------------------

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
        ttl: int,
        no_cache: bool,
        provider_name: str | None,
        fetch_fn: Callable[[BaseProvider], T],
        fresh: bool = False,
        symbol: str | None = None,
        secondary_get: Callable[[], T | None] | None = None,
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
        request_id = uuid.uuid4().hex[:12]
        if lkg_key is None:
            lkg_key = cache_key

        # 1. Cache check (skip if no_cache)
        if not no_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug("Cache hit for %s", cache_key)
                self._audit_recorder.record_cache_hit(
                    request_id=request_id,
                    endpoint=endpoint,
                    cache_key=cache_key,
                    symbol=symbol,
                )
                return cast(T, cached)

            # 1b. Secondary lookup (e.g. slice a cached superset range)
            if secondary_get is not None:
                alt = secondary_get()
                if alt is not None:
                    logger.debug("Cache hit (secondary) for %s", cache_key)
                    self._audit_recorder.record_cache_hit(
                        request_id=request_id,
                        endpoint=endpoint,
                        cache_key=cache_key,
                        symbol=symbol,
                    )
                    return alt

        # Last-known-good (stale-on-error) TTL for this endpoint, if eligible.
        stale_cfg = self._config.stale
        lkg_ttl = stale_cfg.ttl_for(endpoint) if stale_cfg.enabled else None

        # 2. Router dispatch
        try:
            result = self._router.dispatch(
                endpoint,
                fetch_fn,
                fresh=fresh,
                provider_name=provider_name,
                symbol=symbol,
            )
        except AllProvidersFailedError:
            # Availability fallback: serve the last-known-good copy if one is
            # still within its staleness bound. Never bypass a live provider —
            # only consulted once every provider has failed.
            if lkg_ttl is not None:
                lkg = self._cache.get_last_known_good(lkg_key)
                if lkg is not None:
                    logger.warning(
                        "All providers failed for %s; serving stale last-known-good",
                        endpoint,
                    )
                    self._audit_recorder.record_stale_serve(
                        request_id=request_id,
                        endpoint=endpoint,
                        cache_key=cache_key,
                        symbol=symbol,
                        stale_age_s=_lkg_age_seconds(lkg),
                    )
                    return cast(T, lkg)
            raise

        # 3. Cache the result (always write — no_cache only skips reads)
        self._cache.set(cache_key, result, ttl=ttl, tag=endpoint)
        if on_store is not None:
            on_store(result)
        # 3b. Dual-write the long-lived last-known-good copy for stale-on-error.
        if lkg_ttl is not None:
            self._cache.set_last_known_good(lkg_key, result, ttl=lkg_ttl, tag=endpoint)

        return cast(T, result)

    def _cached_batch_fetch(
        self,
        *,
        symbols: list[str],
        endpoint: str,
        data_type: str,
        ttl: int,
        no_cache: bool,
        provider_name: str | None,
        fetch_fn: Callable[[BaseProvider, list[str]], list[Any]],
    ) -> list[Any]:
        """Check cache per symbol, dispatch misses as a batch, cache responses per symbol.

        Returns results in the exact order requested. If the batch fetch fails,
        returns the exception in place of the results for the missing symbols.
        """
        request_id = uuid.uuid4().hex[:12]

        results: dict[str, Any] = {}
        missing_symbols: list[str] = []

        # 1. Check cache for each symbol
        for sym in symbols:
            cache_key = make_key(data_type, symbol=sym)
            if not no_cache:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    results[sym] = cached
                    self._audit_recorder.record_cache_hit(
                        request_id=request_id,
                        endpoint=endpoint,
                        cache_key=cache_key,
                        symbol=sym,
                    )
                    continue
            missing_symbols.append(sym)

        # 2. Dispatch the misses
        if missing_symbols:
            try:
                # The router will pass missing_symbols to fetch_fn
                batch_result = self._router.dispatch(
                    endpoint,
                    lambda p: fetch_fn(p, missing_symbols),
                    fresh=False,
                    provider_name=provider_name,
                    symbol=",".join(missing_symbols[:5])
                    + ("..." if len(missing_symbols) > 5 else ""),
                )

                n_got = len(batch_result)
                if n_got != len(missing_symbols):
                    logger.warning(
                        "Batch %s mismatch: requested %d, got %d",
                        data_type,
                        len(missing_symbols),
                        n_got,
                    )

                # 3. Cache the results (always write — no_cache only skips reads)
                for sym, item in zip(missing_symbols, batch_result):
                    results[sym] = item
                    self._cache.set(make_key(data_type, symbol=sym), item, ttl=ttl, tag=data_type)

                # Symbols truncated by a short batch_result get an error result
                for sym in missing_symbols[n_got:]:
                    results[sym] = FinanceError(
                        "BATCH_RESULT_MISSING",
                        f"No result returned by provider for {sym}",
                    )

            except FinanceError as exc:
                # If the batch fetch failed, put the exception in the results
                # for all missing symbols
                for sym in missing_symbols:
                    results[sym] = exc

        # 4. Stitch back together in original order
        return [results[sym] for sym in symbols]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lkg_age_seconds(lkg: Any) -> float | None:
    """Age in seconds of a served last-known-good value, or None if undeterminable.

    Handles every LKG shape: a single model, a list of models, or an empty
    list. For a list, the newest ``fetched_at`` is used (the dataset's
    capture time). Returns None when no ``fetched_at`` is available (e.g. an
    empty-list result), so the stale serve is still recorded.
    """
    items = lkg if isinstance(lkg, list) else [lkg]
    fetched = [f for f in (getattr(item, "fetched_at", None) for item in items) if f is not None]
    if not fetched:
        return None
    newest = max(fetched)
    age = float((datetime.now(UTC) - newest).total_seconds())
    return round(max(age, 0.0), 1)


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
