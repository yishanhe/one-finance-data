"""OneFinanceClient — the public API.

Presents a single uniform interface across providers with transparent
caching and config-driven tier-walking via the ``ProviderRouter``.

See design doc §5 (architecture) and §11 (public API).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

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
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    EarningsRecord,
    FinancialRatios,
    FinanceModel,
    IncomeStatement,
    InsiderTrade,
    PriceBar,
    Quote,
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

        # Initialise the router
        self._router = ProviderRouter(self._provider_map, self._config)

        # Initialise cache (explicit args override config)
        resolved_cache_dir = cache_dir or self._config.cache.dir
        resolved_size_limit = cache_size_limit_gb or self._config.cache.size_limit_gb
        self._cache = CacheManager(
            cache_dir=resolved_cache_dir,
            size_limit_gb=resolved_size_limit,
        )

    def close(self) -> None:
        """Release resources (closes cache)."""
        self._cache.close()

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
        # 1. Cache check (skip if no_cache)
        if not no_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug("Cache hit for %s", cache_key)
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
