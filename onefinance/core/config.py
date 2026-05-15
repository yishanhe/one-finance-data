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
    # Type A
    "price_history": ["fmp", "finnhub", "twelve_data", "yfinance"],
    "financials": ["fmp", "finnhub", "yfinance"],
    "info": ["fmp", "finnhub", "yfinance"],
    "insider_trades": ["fmp", "finnhub"],
    "dcf": ["fmp"],
    # Type B
    "quote": ["fmp", "finnhub", "yfinance"],
    # Type C — two lists: default (free-tier-first) and fresh (premium-first)
    "ratios": {
        "default": ["fmp", "finnhub"],
        "fresh": ["fmp", "finnhub"],
    },
    "earnings": {
        "default": ["fmp", "finnhub"],
        "fresh": ["fmp", "finnhub"],
    },
    # Alternative Data
    "news": ["fmp", "yfinance"],
    "corporate_actions": ["fmp", "finnhub", "yfinance"],
    "institutional_holders": ["fmp", "yfinance"],
    "analyst_data": ["fmp", "finnhub", "yfinance"],
    "forward_estimates": ["fmp", "finnhub", "yfinance"],
    "options_expirations": ["yfinance"],
    "option_chain": ["yfinance"],
    "screen_stocks": ["fmp"],
    "sector_overview": ["yfinance"],
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
    """

    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    tiers: dict[str, list[str] | dict[str, list[str]]] = field(
        default_factory=lambda: dict(DEFAULT_TIERS)
    )
    cache: CacheConfig = field(default_factory=CacheConfig)
    cooldown: CooldownConfig = field(default_factory=CooldownConfig)

    def get_tier_list(
        self, endpoint: str, *, fresh: bool = False
    ) -> list[str]:
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


def load_config(path: str | Path | None = None) -> OneFinanceConfig:
    """Load configuration from a YAML file.

    Falls back to sensible defaults if *path* is ``None`` or
    the file doesn't exist.
    """
    if path is None:
        logger.debug("No config path specified, using defaults")
        return _default_config()

    resolved = Path(path).expanduser()
    if not resolved.exists():
        logger.warning("Config file %s not found, using defaults", resolved)
        return _default_config()

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("PyYAML not installed; using default configuration")
        return _default_config()

    with open(resolved) as f:
        raw = yaml.safe_load(f) or {}

    return _parse_config(raw)


def _default_config() -> OneFinanceConfig:
    """Build the default config with standard provider settings."""
    return OneFinanceConfig(
        providers={
            "fmp": ProviderConfig(name="fmp", api_key_env="FMP_API_KEY", timeout_s=10),
            "finnhub": ProviderConfig(
                name="finnhub", api_key_env="FINNHUB_API_KEY", timeout_s=10
            ),
            "twelve_data": ProviderConfig(
                name="twelve_data", api_key_env="TWELVE_DATA_API_KEY", timeout_s=10
            ),
            "yfinance": ProviderConfig(name="yfinance", timeout_s=15),
        },
        tiers=dict(DEFAULT_TIERS),
        cache=CacheConfig(),
        cooldown=CooldownConfig(),
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

    return OneFinanceConfig(
        providers=providers,
        tiers=tiers,
        cache=cache,
        cooldown=cooldown,
    )
