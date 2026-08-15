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
    # Free-tier-first ordering: keyless/free-working providers lead the endpoints where
    # the paid providers reject free-plan keys (FMP 402 "not in your subscription",
    # Finnhub 403 on /stock/candle). yfinance is keyless and serves daily bars / delayed
    # quotes / news reliably, so it leads those; paid providers stay in the tier as
    # fallbacks (and move to the front automatically for anyone whose keys actually work,
    # via a config override). See audit-driven rationale.
    # Type A
    "price_history": ["yfinance", "fmp", "massive", "alpha_vantage", "twelve_data", "finnhub"],
    # EDGAR is keyless + SEC-authoritative; it NotSupporteds cleanly for ETFs/foreign
    # filers and falls through to fmp, so it is safe to lead with for free users.
    "financials": ["edgar", "fmp", "finnhub", "alpha_vantage", "yfinance"],
    "info": ["fmp", "finnhub", "massive", "alpha_vantage", "twelve_data", "yfinance"],
    "infos": ["fmp", "finnhub", "massive", "alpha_vantage", "twelve_data", "yfinance"],
    "insider_trades": ["fmp", "finnhub", "yfinance", "alpha_vantage"],
    "dcf": ["fmp"],
    # Type B — Finnhub leads quote: free tier serves real-time-ish quotes (no 15-min
    # delay), yfinance backs it up, and FMP (free-plan 402 + tight 250/day quota) moves
    # to the back so it stops burning quota at tier-0.
    "quote": ["finnhub", "cboe", "yfinance", "massive", "alpha_vantage", "fmp"],
    # Type C — two lists: default (free-tier-first) and fresh (premium-first)
    "ratios": {
        "default": ["fmp", "finnhub", "yfinance", "alpha_vantage"],
        "fresh": ["fmp", "finnhub", "yfinance", "alpha_vantage"],
    },
    "earnings": {
        "default": ["fmp", "finnhub", "alpha_vantage", "yfinance", "twelve_data"],
        "fresh": ["fmp", "finnhub", "yfinance", "twelve_data"],
    },
    # Alternative Data
    "news": ["yfinance", "fmp", "massive", "alpha_vantage", "twelve_data"],
    "corporate_actions": ["fmp", "finnhub", "massive", "yfinance", "alpha_vantage"],
    "institutional_holders": ["fmp", "yfinance"],
    "analyst_data": ["fmp", "finnhub", "yfinance"],
    "forward_estimates": ["yfinance", "fmp", "finnhub"],
    # yfinance is keyless; Massive needs a paid options subscription.
    "options_expirations": ["yfinance", "massive"],
    "option_chain": ["yfinance", "massive"],
    "screen_stocks": ["fmp", "massive"],
    "sector_overview": ["yfinance", "fmp"],
    "earnings_calendar": ["fmp", "finnhub", "alpha_vantage"],
    "economic_calendar": ["finnhub", "fmp"],
    "treasury_rates": ["fmp"],
    "short_interest": ["fmp", "yfinance"],
    "market_sentiment": ["fmp"],
    "peers": ["fmp", "finnhub"],
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

    ``timeout_s`` bounds the *total* wall-clock time spent augmenting one
    result. Augment is opportunistic enrichment — audit data showed filler
    calls stalling up to 17.6 s to fill a single ``volume`` field, making a
    fast primary slower than the filler alone. Past the budget the primary
    result is returned unaugmented; the in-flight filler call still
    completes in the background and its result is written to the augment
    cache so the next request gets the field for free.
    """

    enabled: bool = True
    # Total budget (seconds) for all filler calls on one result.
    timeout_s: float = 2.0
    # endpoint → fields that trigger augmentation when None or 0
    fields: dict[str, list[str]] = field(
        default_factory=lambda: {
            "quote": ["volume"],
        }
    )
    # endpoint → fields merged opportunistically from a filler response we
    # already hold, but which never *trigger* a filler call on their own.
    #
    # The quote path is finnhub-primary + yfinance-filler on ~92% of requests
    # (audit: 327 augments / 355 quote requests). That filler response already
    # carries bid, ask, market_cap and navPrice — dropping them because only
    # ``volume`` is listed above throws away data already paid for. Keeping them
    # out of ``fields`` matters: ``fields`` is the trigger set, so listing e.g.
    # ``bid`` there would make an index quote (Yahoo returns bid 0.0 for ^VIX)
    # fire filler calls down the rest of the tier chasing a bid nobody has.
    extra_fields: dict[str, list[str]] = field(
        default_factory=lambda: {
            "quote": ["bid", "ask", "market_cap", "nav"],
        }
    )


@dataclass
class StaleConfig:
    """Stale-on-error fallback — serve last-known-good data when all providers fail.

    On every successful fetch the client dual-writes a long-lived
    "last-known-good" (LKG) copy of the result. If a later request exhausts
    every provider (raising ``AllProvidersFailedError``), the client serves
    the LKG copy instead of propagating the error — trading absolute
    freshness for availability.

    Only endpoints listed in ``ttls`` are eligible, and the LKG TTL bounds
    the maximum staleness: once the LKG entry expires the error propagates
    as normal, so the served data is never older than its TTL. Fast-moving,
    price-sensitive endpoints (``quote``, ``option_chain``, ``price_history``)
    are intentionally omitted — serving a stale price as if current would
    mislead. The served model's ``fetched_at`` still reflects the original
    fetch time, so consumers can always see the age, and the stale serve is
    recorded in the audit log.
    """

    enabled: bool = True
    # endpoint → max-staleness TTL (seconds) for the LKG copy.
    # Only slow-moving endpoints where stale-but-available beats erroring.
    ttls: dict[str, int] = field(
        default_factory=lambda: {
            "info": 90 * 24 * 3600,  # company profile barely moves
            "financials": 30 * 24 * 3600,
            "ratios": 7 * 24 * 3600,
            "earnings": 30 * 24 * 3600,
            "dcf": 30 * 24 * 3600,
            "corporate_actions": 30 * 24 * 3600,
            "institutional_holders": 30 * 24 * 3600,
            "analyst_data": 7 * 24 * 3600,
            "forward_estimates": 14 * 24 * 3600,
            "sector_overview": 7 * 24 * 3600,
            "news": 3 * 24 * 3600,
            "peers": 30 * 24 * 3600,
            "treasury_rates": 7 * 24 * 3600,
        }
    )

    def ttl_for(self, endpoint: str) -> int | None:
        """Return the LKG TTL for *endpoint*, or None if not eligible."""
        return self.ttls.get(endpoint)


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
    stale: StaleConfig = field(default_factory=StaleConfig)
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
            "massive": ProviderConfig(name="massive", api_key_env="MASSIVE_API_KEY", timeout_s=10),
            "edgar": ProviderConfig(name="edgar", timeout_s=15),
            "cboe": ProviderConfig(name="cboe", timeout_s=10),
        },
        tiers=dict(DEFAULT_TIERS),
        cache=CacheConfig(),
        cooldown=CooldownConfig(),
        fallback_order=["yfinance"],
    )


def default_config_template() -> str:
    """Render a YAML template from the same defaults used at runtime."""
    import yaml

    config = _default_config()
    data = {
        "providers": {
            name: {
                **({"api_key_env": provider.api_key_env} if provider.api_key_env else {}),
                "timeout_s": provider.timeout_s,
            }
            for name, provider in config.providers.items()
        },
        "tiers": config.tiers,
        "fallback_order": config.fallback_order,
        "cache": {
            "dir": config.cache.dir,
            "size_limit_gb": config.cache.size_limit_gb,
        },
        "cooldown": {
            "default_initial_s": config.cooldown.default_initial_s,
            "max_backoff_s": config.cooldown.max_backoff_s,
        },
        "augment": {
            "enabled": config.augment.enabled,
            "timeout_s": config.augment.timeout_s,
            "fields": config.augment.fields,
            "extra_fields": config.augment.extra_fields,
        },
    }
    rendered: str = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
    )
    return "# OneFinance configuration\n" + rendered


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

    augment_raw = raw.get("augment", {})
    augment = AugmentConfig(
        enabled=bool(augment_raw.get("enabled", True)),
        timeout_s=float(augment_raw.get("timeout_s", 2.0)),
        fields={
            str(k): list(v) for k, v in augment_raw.get("fields", {"quote": ["volume"]}).items()
        },
        extra_fields={
            str(k): list(v)
            for k, v in augment_raw.get(
                "extra_fields",
                {"quote": ["bid", "ask", "market_cap", "nav"]},
            ).items()
        },
    )

    fallback_order: list[str] = raw.get("fallback_order", ["yfinance"])

    # Stale-on-error fallback
    stale_raw = raw.get("stale", {})
    stale = StaleConfig()
    if "enabled" in stale_raw:
        stale.enabled = bool(stale_raw["enabled"])
    if "ttls" in stale_raw:
        stale.ttls = {str(k): int(v) for k, v in stale_raw["ttls"].items()}

    return OneFinanceConfig(
        providers=providers,
        tiers=tiers,
        cache=cache,
        cooldown=cooldown,
        augment=augment,
        stale=stale,
        fallback_order=fallback_order,
    )
