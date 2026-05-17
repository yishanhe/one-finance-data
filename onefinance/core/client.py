"""OneFinanceClient — the public API.

Presents a single uniform interface across providers with transparent
caching and config-driven tier-walking via the ``ProviderRouter``.

See design doc §5 (architecture) and §11 (public API).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from onefinance.indicators.core import TechnicalIndicators

from onefinance.audit.log import AuditLog
from onefinance.audit.models import AuditEntry, AuditStats

from onefinance.cache.keys import make_key
from onefinance.cache.manager import (
    CacheManager,
    default_ttl,
    ttl_for_price_history,
)
from onefinance.core.config import OneFinanceConfig, load_config
from onefinance.core.errors import (
    InvalidArgumentError,
)
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    DCFValuation,
    EarningsRecord,
    FinancialRatios,
    FinanceModel,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    NewsArticle,
    OptionChain,
    PriceBar,
    Quote,
    ScreenerResult,
    SectorInfo,
)
from onefinance.core.router import ProviderRouter
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)


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

        # Initialise audit log
        self._audit = AuditLog(
            log_path=audit_log_path,
            retention_days=audit_retention_days,
            enabled=audit,
        )

        # Initialise the router (with audit log)
        self._router = ProviderRouter(
            self._provider_map, self._config, audit_log=self._audit
        )

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
            self._config, self._provider_map,
            ping=ping, ping_symbol=ping_symbol,
            ping_timeout_s=ping_timeout_s, only=only,
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
            raise InvalidArgumentError(
                f"start ({start_d}) must be <= end ({end_d})"
            )

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
        effective_ttl = ttl if ttl is not None else default_ttl("info")

        result = self._cached_fetch(
            cache_key=cache_key,
            endpoint="info",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_info(symbol.upper()),
        )
        if isinstance(result, list):
            return result[0]  # type: ignore[return-value]
        return result  # type: ignore[return-value]

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
        effective_ttl = ttl if ttl is not None else default_ttl("financials")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="financials",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
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
        effective_ttl = ttl if ttl is not None else default_ttl("insider_trades")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="insider_trades",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
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
        effective_ttl = ttl if ttl is not None else default_ttl("quote")

        result = self._cached_fetch(
            cache_key=cache_key,
            endpoint="quote",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_quote(symbol.upper()),
        )
        if isinstance(result, list):
            return result[0]  # type: ignore[return-value]
        return result  # type: ignore[return-value]

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
        effective_ttl = ttl if ttl is not None else default_ttl("ratios", fresh=fresh)

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="ratios",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fresh=fresh,
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
        effective_ttl = ttl if ttl is not None else default_ttl("earnings", fresh=fresh)

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="earnings",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fresh=fresh,
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
        effective_ttl = ttl if ttl is not None else default_ttl("dcf")

        result = self._cached_fetch(
            cache_key=cache_key,
            endpoint="dcf",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_dcf(symbol.upper()),
        )
        if isinstance(result, list):
            return result[0]  # type: ignore[return-value]
        return result  # type: ignore[return-value]

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
            symbol, start=start_d, end=end_d, interval=interval,
            no_cache=no_cache, provider=provider, ttl=ttl,
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
        effective_ttl = ttl if ttl is not None else default_ttl("news")
        
        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="news",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_news(symbol.upper(), limit=limit),
        )  # type: ignore[no-any-return]

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
        effective_ttl = ttl if ttl is not None else default_ttl("corporate_actions")
        
        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="corporate_actions",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_corporate_actions(symbol.upper()),
        )  # type: ignore[no-any-return]

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
        effective_ttl = ttl if ttl is not None else default_ttl("institutional_holders")
        
        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="institutional_holders",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_institutional_holders(symbol.upper()),
        )  # type: ignore[no-any-return]

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
        effective_ttl = ttl if ttl is not None else default_ttl("analyst_data")
        
        result = self._cached_fetch(
            cache_key=cache_key,
            endpoint="analyst_data",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_analyst_data(symbol.upper()),
        )
        if isinstance(result, list):
            return result[0]  # type: ignore[no-any-return,return-value]
        return result  # type: ignore[no-any-return,return-value]

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
        effective_ttl = ttl if ttl is not None else default_ttl("options_expirations")
        
        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="options_expirations",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_options_expirations(symbol.upper()),
        )  # type: ignore[no-any-return]

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
        effective_ttl = ttl if ttl is not None else default_ttl("option_chain")
        
        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="option_chain",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_option_chain(symbol.upper(), expiration),
        )  # type: ignore[no-any-return]

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
        effective_ttl = ttl if ttl is not None else default_ttl("screen_stocks")
        
        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="screen_stocks",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.screen_stocks(query),
        )  # type: ignore[no-any-return]

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
        effective_ttl = ttl if ttl is not None else default_ttl("sector_overview")
        
        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="sector_overview",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_sector_overview(sector),
        )  # type: ignore[no-any-return]

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
        effective_ttl = ttl if ttl is not None else default_ttl("forward_estimates")

        return self._cached_fetch(
            cache_key=cache_key,
            endpoint="forward_estimates",
            ttl=effective_ttl,
            no_cache=no_cache,
            provider_name=provider,
            fetch_fn=lambda p: p.get_forward_estimates(symbol),
        )  # type: ignore[no-any-return]

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
        fetch_fn: Any,
        fresh: bool = False,
    ) -> Any:
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
                # Record cache hit in audit log
                if self._audit.enabled:
                    self._audit.record(AuditEntry(
                        timestamp=datetime.now(timezone.utc),
                        request_id=request_id,
                        endpoint=endpoint,
                        provider="cache",
                        status="cache_hit",
                        latency_ms=0.0,
                        cache_key=cache_key,
                    ))
                return cached

        # 2. Router dispatch
        result = self._router.dispatch(
            endpoint,
            fetch_fn,
            fresh=fresh,
            provider_name=provider_name,
        )

        # 3. Cache the result (skip if no_cache)
        if not no_cache:
            self._cache.set(cache_key, result, ttl=ttl, tag=endpoint)

        return result


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

    Iterates the config's provider registry in definition order, silently
    skipping any provider whose API key env var is unset or whose package
    is not installed.  Always returns at least YFinanceProvider (no key
    required) so the client never starts empty-handed.
    """
    providers: list[BaseProvider] = []
    for name, prov_cfg in config.providers.items():
        try:
            p = _instantiate_provider(name, prov_cfg)
            if p is not None:
                providers.append(p)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipping provider %r: %s", name, exc)
    if not providers:
        from onefinance.providers.yfinance_provider import YFinanceProvider
        providers = [YFinanceProvider()]
    return providers


def _instantiate_provider(name: str, prov_cfg: Any) -> "BaseProvider | None":
    """Return a provider instance for *name*, or ``None`` if the key is absent."""
    from onefinance.core.config import ProviderConfig  # local to avoid circular

    if not isinstance(prov_cfg, ProviderConfig):
        return None

    if name == "fmp":
        key = prov_cfg.api_key
        if not key:
            return None
        from onefinance.providers.fmp import FMPProvider
        return FMPProvider(api_key=key, timeout=prov_cfg.timeout_s)

    if name == "finnhub":
        key = prov_cfg.api_key
        if not key:
            return None
        from onefinance.providers.finnhub import FinnhubProvider
        return FinnhubProvider(api_key=key, timeout=prov_cfg.timeout_s)

    if name == "twelve_data":
        key = prov_cfg.api_key
        if not key:
            return None
        from onefinance.providers.twelve_data import TwelveDataProvider
        return TwelveDataProvider(api_key=key, timeout=prov_cfg.timeout_s)

    if name == "yfinance":
        from onefinance.providers.yfinance_provider import YFinanceProvider
        return YFinanceProvider()

    logger.debug("No factory registered for provider %r — skipping", name)
    return None
