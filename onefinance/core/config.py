"""Configuration loader for OneFinance.

Loads provider settings and tier tables from a YAML config file,
with sensible defaults when no config is provided.

See design doc §6 (tier strategy) and §13 (configuration).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default tier tables — per design doc §6
# ---------------------------------------------------------------------------

# Type A — historical, cache-heavy, free-tier-first
# Type B — always current, prefer realtime-capable providers
# Type C — caller decides via fresh=

DEFAULT_TIERS: dict[str, list[str] | dict[str, list[str]]] = {
    # Type A — yfinance is last resort; alpha_vantage before yfinance (very tight quota but
    # more authoritative than the unofficial yfinance scraper)
    "price_history": ["fmp", "twelve_data", "finnhub", "polygon", "alpha_vantage", "yfinance"],
    "financials": ["fmp", "finnhub", "alpha_vantage", "yfinance"],
    "info": ["fmp", "finnhub", "polygon", "alpha_vantage", "yfinance"],
    "insider_trades": ["fmp", "finnhub", "yfinance"],
    "dcf": ["fmp"],
    # Type B — yfinance last: unofficial scraper, no real-time guarantee
    # AV before yfinance: 15-min delay but API-backed; yfinance = ultimate fallback
    "quote": ["fmp", "finnhub", "polygon", "alpha_vantage", "yfinance"],
    # Type C — two lists: default (free-tier-first) and fresh (premium-first)
    "ratios": {
        "default": ["fmp", "finnhub", "yfinance", "alpha_vantage"],
        "fresh": ["fmp", "finnhub", "yfinance", "alpha_vantage"],
    },
    "earnings": {
        "default": ["fmp", "finnhub", "alpha_vantage", "yfinance"],
        "fresh": ["fmp", "finnhub", "yfinance"],
    },
    # Alternative Data
    "news": ["fmp", "polygon", "alpha_vantage", "yfinance"],
    "corporate_actions": ["fmp", "finnhub", "polygon", "yfinance"],
    "institutional_holders": ["fmp", "yfinance"],
    "analyst_data": ["fmp", "finnhub", "yfinance"],
    "forward_estimates": ["fmp", "finnhub", "yfinance"],
    "options_expirations": ["yfinance"],
    "option_chain": ["yfinance"],
    "screen_stocks": ["fmp"],
    "sector_overview": ["yfinance"],
    "earnings_calendar": ["fmp", "finnhub"],
    "short_interest": ["fmp", "yfinance"],
    "market_sentiment": ["fmp"],
}


@dataclass
class ProviderConfig:
    """Settings for a single provider."""

    name: str
    api_key_env: str | None = None
    timeout_s: int = 10

    @property
    def api_key(self) -> str | None:
        """Resolve API key from environment."""
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None


@dataclass
class CooldownConfig:
    """Cooldown settings for the router."""

    default_initial_s: float = 60.0
    max_backoff_s: float = 3600.0


@dataclass
class AugmentConfig:
    """Null-fill merge — call secondary providers to fill None/0 fields.

    After the primary provider returns a result, if any augmentable fields
    are None or zero, the router tries remaining providers to fill those
    gaps and merges the result.  ``source`` becomes ``"primary+filler"``.
    """

    enabled: bool = True
    # endpoint → fields that trigger augmentation when None or 0
    fields: dict[str, list[str]] = field(
        default_factory=lambda: {
            "quote": ["volume"],
        }
    )


@dataclass
class CacheConfig:
    """Cache settings."""

    dir: str = "~/.one_finance_data/cache"
    size_limit_gb: float = 2.0
    ttl_overrides: dict[str, int] = field(default_factory=dict)


@dataclass
class OneFinanceConfig:
    """Complete OneFinance configuration.

    Parameters
    ----------
    providers:
        Per-provider settings (API key env var, timeout, etc.).
    tiers:
        Endpoint → provider tier list(s).  For Type A/B endpoints,
        the value is ``list[str]``.  For Type C endpoints, it's
        ``{"default": [...], "fresh": [...]}``.
    cache:
        Cache directory, size limit, TTL overrides.
    cooldown:
        Backoff settings for the provider router.
    fallback_order:
        Global provider fallback chain.  After the endpoint-specific tier
        list is exhausted, any provider in *fallback_order* that was not
        already tried is appended (in order) as a last resort.  Defaults
        to ``["yfinance"]`` so yfinance is always the ultimate fallback
        for any endpoint it supports.  Pass ``[]`` to disable.
    """

    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    tiers: dict[str, list[str] | dict[str, list[str]]] = field(
        default_factory=lambda: dict(DEFAULT_TIERS)
    )
    cache: CacheConfig = field(default_factory=CacheConfig)
    cooldown: CooldownConfig = field(default_factory=CooldownConfig)
    augment: AugmentConfig = field(default_factory=AugmentConfig)
    fallback_order: list[str] = field(default_factory=lambda: ["yfinance"])

    def get_tier_list(self, endpoint: str, *, fresh: bool = False) -> list[str]:
        """Return the provider tier list for the given endpoint.

        For Type C endpoints with ``fresh=True``, returns the ``fresh``
        sub-list.  Falls back to the ``default`` sub-list or the plain
        list for Type A/B.
        """
        entry = self.tiers.get(endpoint)
        if entry is None:
            return []
        if isinstance(entry, dict):
            key = "fresh" if fresh else "default"
            return list(entry.get(key, entry.get("default", [])))
        return list(entry)

    def flat_tier_refs(self) -> dict[str, list[str]]:
        """Flatten tier entries into ``{endpoint: [provider, ...]}``.

        Type A/B values (``list[str]``) are returned as-is; Type C values
        (``{"default": [...], "fresh": [...]}``) collapse to a single
        deduplicated, order-preserving provider list.
        """
        out: dict[str, list[str]] = {}
        for endpoint, entry in self.tiers.items():
            if isinstance(entry, dict):
                names: list[str] = []
                for sub in entry.values():
                    if isinstance(sub, list):
                        names.extend(sub)
                out[endpoint] = list(dict.fromkeys(names))
            elif isinstance(entry, list):
                out[endpoint] = list(entry)
        return out


def load_config(path: str | Path | None = None) -> OneFinanceConfig:
    """Load configuration from a YAML file.

    Falls back to sensible defaults if *path* is ``None`` or
    the file doesn't exist.

    The ``OFCLIENT_FALLBACK_ORDER`` environment variable (comma-separated
    provider names) overrides whatever is in the YAML or the defaults, so
    operators can tune the fallback chain without touching config files.
    """
    if path is None:
        logger.debug("No config path specified, using defaults")
        config = _default_config()
    elif not (resolved := Path(path).expanduser()).exists():
        logger.warning("Config file %s not found, using defaults", resolved)
        config = _default_config()
    else:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("PyYAML not installed; using default configuration")
            config = _default_config()
        else:
            with open(resolved) as f:
                raw = yaml.safe_load(f) or {}
            config = _parse_config(raw)

    # Env var overrides YAML / default — highest precedence.
    fallback_env = os.environ.get("OFCLIENT_FALLBACK_ORDER")
    if fallback_env is not None:
        config.fallback_order = [p.strip() for p in fallback_env.split(",") if p.strip()]
        logger.debug("fallback_order set from env: %s", config.fallback_order)

    return config


def _default_config() -> OneFinanceConfig:
    """Build the default config with standard provider settings."""
    return OneFinanceConfig(
        providers={
            "fmp": ProviderConfig(name="fmp", api_key_env="FMP_API_KEY", timeout_s=10),
            "finnhub": ProviderConfig(name="finnhub", api_key_env="FINNHUB_API_KEY", timeout_s=10),
            "twelve_data": ProviderConfig(
                name="twelve_data", api_key_env="TWELVE_DATA_API_KEY", timeout_s=10
            ),
            "yfinance": ProviderConfig(name="yfinance", timeout_s=15),
            "alpha_vantage": ProviderConfig(
                name="alpha_vantage", api_key_env="ALPHAVANTAGE_API_KEY", timeout_s=10
            ),
            "polygon": ProviderConfig(name="polygon", api_key_env="POLYGON_API_KEY", timeout_s=10),
        },
        tiers=dict(DEFAULT_TIERS),
        cache=CacheConfig(),
        cooldown=CooldownConfig(),
        fallback_order=["yfinance"],
    )


def _parse_config(raw: dict[str, Any]) -> OneFinanceConfig:
    """Parse a raw YAML dict into a OneFinanceConfig."""
    # Providers
    providers: dict[str, ProviderConfig] = {}
    for name, settings in raw.get("providers", {}).items():
        if isinstance(settings, dict):
            providers[name] = ProviderConfig(
                name=name,
                api_key_env=settings.get("api_key_env"),
                timeout_s=settings.get("timeout_s", 10),
            )
        else:
            providers[name] = ProviderConfig(name=name)

    # Use defaults for any providers not specified
    defaults = _default_config()
    for name, prov in defaults.providers.items():
        if name not in providers:
            providers[name] = prov

    # Tiers
    tiers = raw.get("tiers", dict(DEFAULT_TIERS))

    # Cache
    cache_raw = raw.get("cache", {})
    cache = CacheConfig(
        dir=cache_raw.get("dir", "~/.one_finance_data/cache"),
        size_limit_gb=cache_raw.get("size_limit_gb", 2.0),
        ttl_overrides=cache_raw.get("ttl_overrides", {}),
    )

    # Cooldown
    cd_raw = raw.get("cooldown", {})
    cooldown = CooldownConfig(
        default_initial_s=cd_raw.get("default_initial_s", 60.0),
        max_backoff_s=cd_raw.get("max_backoff_s", 3600.0),
    )

    fallback_order: list[str] = raw.get("fallback_order", ["yfinance"])

    return OneFinanceConfig(
        providers=providers,
        tiers=tiers,
        cache=cache,
        cooldown=cooldown,
        fallback_order=fallback_order,
    )
