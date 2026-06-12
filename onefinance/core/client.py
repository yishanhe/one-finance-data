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
    ttl_for_price_history,
)
from onefinance.core.config import OneFinanceConfig, load_config
from onefinance.core.errors import (
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
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    MarketSentiment,
    NewsArticle,
    OptionChain,
    OptionsAnalytics,
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
    ) -> None:
        # Load config
        if isinstance(config, OneFinanceConfig):
            self._config = config
        else:
            self._config = load_config(config)

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

        # Initialise the router (with audit log)
        self._router = ProviderRouter(self._provider_map, self._config, audit_log=self._audit)

        # Initialise cache (explicit args override config)
        resolved_cache_dir = cache_dir or self._config.cache.dir
        resolved_size_limit = cache_size_limit_gb or self._config.cache.size_limit_gb
        self._cache = CacheManager(
            cache_dir=resolved_cache_dir,
            size_limit_gb=resolved_size_limit,
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

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="price_history",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_price_history(symbol.upper(), start_d, end_d, interval),
        )

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
        cache_key = make_key(
            "financials",
            symbol=symbol.upper(),
            statement=statement,
            period=period,
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
        cache_key = make_key(
            "insider_trades",
            symbol=symbol.upper(),
            since=since_d,
        )
        effective_ttl = ttl if ttl is not None else self._default_ttl("insider_trades")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="insider_trades",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_insider_trades(symbol.upper(), since_d),
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

        Type B endpoint — always fetched with short TTL (30s).
        """
        cache_key = make_key("quote", symbol=symbol.upper())
        effective_ttl = ttl if ttl is not None else self._default_ttl("quote")

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

        effective_ttl = ttl if ttl is not None else self._default_ttl("quote")
        normalized = [s.upper() for s in symbols]

        return self._cached_batch_fetch(
            symbols=normalized,
            endpoint="quotes",
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
        cache_key = make_key(
            "ratios",
            symbol=symbol.upper(),
            period=period,
            fresh=fresh,
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
        cache_key = make_key(
            "earnings",
            symbol=symbol.upper(),
            fresh=fresh,
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
        cache_key = make_key("news", symbol=symbol.upper(), limit=limit)
        effective_ttl = ttl if ttl is not None else self._default_ttl("news")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="news",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_news(symbol.upper(), limit=limit),
        )

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
        effective_ttl = ttl if ttl is not None else self._default_ttl("option_chain")

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
        cache_key = make_key("screen_stocks", query=query)
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
        )

        if symbol:
            sym_upper = symbol.upper()
            results = [e for e in results if e.symbol == sym_upper]

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
        cache_key = make_key("estimates", symbol=symbol)
        effective_ttl = ttl if ttl is not None else self._default_ttl("forward_estimates")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="forward_estimates",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            symbol=symbol.upper(),
            fetch_fn=lambda p: p.get_forward_estimates(symbol),
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
    ) -> T:
        """Check cache, then dispatch via the provider router.

        The router handles tier walking, cooldown management, and
        fallback logic.
        """
        request_id = uuid.uuid4().hex[:12]

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

        # 2. Router dispatch
        result = self._router.dispatch(
            endpoint,
            fetch_fn,
            fresh=fresh,
            provider_name=provider_name,
            symbol=symbol,
        )

        # 3. Cache the result (skip if no_cache)
        if not no_cache:
            self._cache.set(cache_key, result, ttl=ttl, tag=endpoint)

        return cast(T, result)

    def _cached_batch_fetch(
        self,
        *,
        symbols: list[str],
        endpoint: str,
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
            cache_key = make_key("quote", symbol=sym)
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

                # We expect the provider to return a list of items matching the missing_symbols
                if len(batch_result) != len(missing_symbols):
                    logger.warning(
                        "Batch quote mismatch: requested %d, got %d",
                        len(missing_symbols),
                        len(batch_result),
                    )

                # 3. Cache the results
                for sym, item in zip(missing_symbols, batch_result):
                    results[sym] = item
                    if not no_cache:
                        self._cache.set(make_key("quote", symbol=sym), item, ttl=ttl, tag=endpoint)

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
