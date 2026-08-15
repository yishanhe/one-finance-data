"""Tests for onefinance.core.config — YAML loader and defaults."""

from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from onefinance.core.config import (
    DEFAULT_TIERS,
    OneFinanceConfig,
    ProviderConfig,
    _default_config,
    _parse_config,
    default_config_template,
    load_config,
)

# ---------------------------------------------------------------------------
# ProviderConfig
# ---------------------------------------------------------------------------


class TestProviderConfig:
    """Tests for ProviderConfig."""

    def test_api_key_from_env(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_KEY_ENV", "my-secret-key")
        config = ProviderConfig(name="test", api_key_env="TEST_KEY_ENV")
        assert config.api_key == "my-secret-key"

    def test_api_key_missing_env(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.delenv("NONEXISTENT_KEY", raising=False)
        config = ProviderConfig(name="test", api_key_env="NONEXISTENT_KEY")
        assert config.api_key is None

    def test_no_api_key_env(self) -> None:
        config = ProviderConfig(name="yfinance")
        assert config.api_key is None


# ---------------------------------------------------------------------------
# OneFinanceConfig — get_tier_list
# ---------------------------------------------------------------------------


class TestOneFinanceConfig:
    """Tests for tier list resolution."""

    def test_type_a_returns_flat_list(self) -> None:
        config = OneFinanceConfig(
            tiers={
                "price_history": ["fmp", "finnhub", "yfinance"],
            }
        )
        assert config.get_tier_list("price_history") == ["fmp", "finnhub", "yfinance"]

    def test_type_c_default(self) -> None:
        config = OneFinanceConfig(
            tiers={
                "ratios": {"default": ["fmp", "finnhub"], "fresh": ["finnhub", "fmp"]},
            }
        )
        assert config.get_tier_list("ratios", fresh=False) == ["fmp", "finnhub"]

    def test_type_c_fresh(self) -> None:
        config = OneFinanceConfig(
            tiers={
                "ratios": {"default": ["fmp", "finnhub"], "fresh": ["finnhub", "fmp"]},
            }
        )
        assert config.get_tier_list("ratios", fresh=True) == ["finnhub", "fmp"]

    def test_unknown_endpoint_returns_empty(self) -> None:
        config = OneFinanceConfig(tiers={})
        assert config.get_tier_list("nonexistent") == []

    def test_parse_augment_settings(self) -> None:
        config = _parse_config(
            {
                "augment": {
                    "enabled": False,
                    "timeout_s": 0.5,
                    "fields": {"quote": ["volume"]},
                    "extra_fields": {"quote": ["bid"]},
                }
            }
        )

        assert config.augment.enabled is False
        assert config.augment.timeout_s == 0.5
        assert config.augment.extra_fields == {"quote": ["bid"]}

    def test_fresh_on_type_a_returns_same_list(self) -> None:
        """Type A endpoints don't have fresh sub-lists; fresh= is ignored."""
        config = OneFinanceConfig(
            tiers={
                "price_history": ["fmp", "yfinance"],
            }
        )
        assert config.get_tier_list("price_history", fresh=True) == ["fmp", "yfinance"]

    def test_returns_copy_not_reference(self) -> None:
        """Tier lists should be copies so callers can't mutate config."""
        config = OneFinanceConfig(
            tiers={
                "price_history": ["fmp", "yfinance"],
            }
        )
        tier_list = config.get_tier_list("price_history")
        tier_list.append("extra")
        assert "extra" not in config.get_tier_list("price_history")


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    """Tests for the default configuration."""

    def test_default_has_expected_providers(self) -> None:
        config = _default_config()
        assert "fmp" in config.providers
        assert "finnhub" in config.providers
        assert "twelve_data" in config.providers
        assert "yfinance" in config.providers
        assert "tradier" not in config.providers

    def test_generated_template_round_trips_runtime_defaults(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(default_config_template())

        generated = load_config(config_path)
        runtime = _default_config()

        assert generated.tiers == DEFAULT_TIERS
        assert generated.fallback_order == runtime.fallback_order
        assert generated.cache == runtime.cache
        assert generated.cooldown == runtime.cooldown
        assert {
            name: (provider.api_key_env, provider.timeout_s)
            for name, provider in generated.providers.items()
        } == {
            name: (provider.api_key_env, provider.timeout_s)
            for name, provider in runtime.providers.items()
        }

    def test_default_tiers_match_design_doc(self) -> None:
        config = _default_config()
        # Type A — free-tier-first: yfinance leads; twelve_data demoted after massive/alpha_vantage
        # (28% rate-limit failure rate caused cascading fallbacks)
        assert config.get_tier_list("price_history") == [
            "yfinance",
            "fmp",
            "massive",
            "alpha_vantage",
            "twelve_data",
            "finnhub",
        ]
        # EDGAR (keyless, SEC-authoritative) leads; falls through for non-filers
        assert config.get_tier_list("financials") == [
            "edgar",
            "fmp",
            "finnhub",
            "alpha_vantage",
            "yfinance",
        ]
        assert config.get_tier_list("info") == [
            "fmp",
            "finnhub",
            "massive",
            "alpha_vantage",
            "twelve_data",
            "yfinance",
        ]
        assert config.get_tier_list("insider_trades") == [
            "fmp",
            "finnhub",
            "yfinance",
            "alpha_vantage",
        ]
        # Type B — Finnhub leads; keyless Cboe covers volatility indexes; FMP demoted off tier-0
        assert config.get_tier_list("quote") == [
            "finnhub",
            "cboe",
            "yfinance",
            "massive",
            "alpha_vantage",
            "fmp",
        ]
        # Type C
        assert config.get_tier_list("ratios", fresh=False) == [
            "fmp",
            "finnhub",
            "yfinance",
            "alpha_vantage",
        ]
        assert config.get_tier_list("ratios", fresh=True) == [
            "fmp",
            "finnhub",
            "yfinance",
            "alpha_vantage",
        ]
        assert config.get_tier_list("earnings", fresh=False) == [
            "fmp",
            "finnhub",
            "alpha_vantage",
            "yfinance",
            "twelve_data",
        ]
        assert config.get_tier_list("earnings", fresh=True) == [
            "fmp",
            "finnhub",
            "yfinance",
            "twelve_data",
        ]
        assert config.get_tier_list("options_expirations") == ["yfinance", "massive"]
        assert config.get_tier_list("option_chain") == ["yfinance", "massive"]

    def test_generated_template_does_not_include_removed_providers(self) -> None:
        assert "tradier" not in default_config_template().lower()

    def test_default_fallback_order_is_yfinance(self) -> None:
        config = _default_config()
        assert config.fallback_order == ["yfinance"]

    def test_default_has_missing_endpoint_tiers(self) -> None:
        config = _default_config()
        assert config.get_tier_list("earnings_calendar") == ["fmp", "finnhub", "alpha_vantage"]
        assert config.get_tier_list("short_interest") == ["fmp", "yfinance"]
        assert config.get_tier_list("market_sentiment") == ["fmp"]

    def test_default_cooldown_values(self) -> None:
        config = _default_config()
        assert config.cooldown.default_initial_s == 60.0
        assert config.cooldown.max_backoff_s == 3600.0

    def test_default_cache_values(self) -> None:
        config = _default_config()
        assert config.cache.dir == "~/.one_finance_data/cache"
        assert config.cache.size_limit_gb == 2.0


# ---------------------------------------------------------------------------
# load_config — YAML file loading
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Tests for YAML config loading."""

    def test_none_path_returns_defaults(self) -> None:
        config = load_config(None)
        assert "fmp" in config.providers
        assert config.get_tier_list("price_history") == [
            "yfinance",
            "fmp",
            "massive",
            "alpha_vantage",
            "twelve_data",
            "finnhub",
        ]

    def test_nonexistent_path_returns_defaults(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "does_not_exist.yaml")
        assert "fmp" in config.providers

    def test_valid_yaml_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
providers:
  fmp:
    api_key_env: MY_FMP_KEY
    timeout_s: 20
  yfinance:
    timeout_s: 30

tiers:
  price_history: [yfinance, fmp]
  quote: [fmp]

cache:
  dir: /tmp/test_cache
  size_limit_gb: 1.0

cooldown:
  default_initial_s: 30
  max_backoff_s: 1800
""")
        config = load_config(config_file)

        # Provider settings
        assert config.providers["fmp"].api_key_env == "MY_FMP_KEY"
        assert config.providers["fmp"].timeout_s == 20
        assert config.providers["yfinance"].timeout_s == 30

        # Tier tables from YAML
        assert config.get_tier_list("price_history") == ["yfinance", "fmp"]
        assert config.get_tier_list("quote") == ["fmp"]

        # Cache
        assert config.cache.dir == "/tmp/test_cache"
        assert config.cache.size_limit_gb == 1.0

        # Cooldown
        assert config.cooldown.default_initial_s == 30.0
        assert config.cooldown.max_backoff_s == 1800.0

    def test_partial_yaml_fills_defaults(self, tmp_path: Path) -> None:
        """Missing sections in YAML → defaults are used."""
        config_file = tmp_path / "partial.yaml"
        config_file.write_text("""
tiers:
  quote: [yfinance]
""")
        config = load_config(config_file)

        # Quote tier from YAML
        assert config.get_tier_list("quote") == ["yfinance"]

        # Default providers still present
        assert "fmp" in config.providers
        assert "yfinance" in config.providers

        # Default cooldown/cache
        assert config.cooldown.default_initial_s == 60.0
        assert config.cache.dir == "~/.one_finance_data/cache"


# ---------------------------------------------------------------------------
# _parse_config — raw dict parsing
# ---------------------------------------------------------------------------


class TestParseConfig:
    """Tests for parsing raw config dicts."""

    def test_empty_dict_returns_defaults(self) -> None:
        config = _parse_config({})
        assert "fmp" in config.providers
        assert config.cooldown.default_initial_s == 60.0

    def test_provider_override(self) -> None:
        config = _parse_config(
            {
                "providers": {
                    "fmp": {"api_key_env": "CUSTOM_FMP", "timeout_s": 5},
                },
            }
        )
        assert config.providers["fmp"].api_key_env == "CUSTOM_FMP"
        assert config.providers["fmp"].timeout_s == 5
        # Others still have defaults
        assert config.providers["yfinance"].timeout_s == 15

    def test_ttl_overrides_in_cache(self) -> None:
        config = _parse_config(
            {
                "cache": {
                    "ttl_overrides": {"quote": 60, "info": 86400},
                },
            }
        )
        assert config.cache.ttl_overrides["quote"] == 60
        assert config.cache.ttl_overrides["info"] == 86400


# ---------------------------------------------------------------------------
# OFCLIENT_FALLBACK_ORDER env var
# ---------------------------------------------------------------------------


class TestFallbackOrderEnvVar:
    """OFCLIENT_FALLBACK_ORDER overrides default and YAML fallback_order."""

    def test_env_var_overrides_default(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("OFCLIENT_FALLBACK_ORDER", "fmp,yfinance")
        config = load_config(None)
        assert config.fallback_order == ["fmp", "yfinance"]

    def test_env_var_overrides_yaml(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("fallback_order: [alpha_vantage]\n")
        monkeypatch.setenv("OFCLIENT_FALLBACK_ORDER", "massive,yfinance")
        config = load_config(config_file)
        assert config.fallback_order == ["massive", "yfinance"]

    def test_env_var_empty_string_yields_empty_list(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("OFCLIENT_FALLBACK_ORDER", "")
        config = load_config(None)
        assert config.fallback_order == []

    def test_env_var_whitespace_stripped(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("OFCLIENT_FALLBACK_ORDER", " fmp , yfinance ")
        config = load_config(None)
        assert config.fallback_order == ["fmp", "yfinance"]

    def test_no_env_var_uses_default(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.delenv("OFCLIENT_FALLBACK_ORDER", raising=False)
        config = load_config(None)
        assert config.fallback_order == ["yfinance"]
