"""Tests for onefinance.core.config — YAML loader and defaults."""

from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from onefinance.core.config import (
    OneFinanceConfig,
    ProviderConfig,
    _default_config,
    _parse_config,
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

    def test_default_has_all_four_providers(self) -> None:
        config = _default_config()
        assert "fmp" in config.providers
        assert "finnhub" in config.providers
        assert "twelve_data" in config.providers
        assert "yfinance" in config.providers

    def test_default_tiers_match_design_doc(self) -> None:
        config = _default_config()
        # Type A
        assert config.get_tier_list("price_history") == [
            "fmp",
            "finnhub",
            "twelve_data",
            "yfinance",
            "alpha_vantage",
        ]
        assert config.get_tier_list("financials") == [
            "fmp",
            "finnhub",
            "alpha_vantage",
            "yfinance",
        ]
        assert config.get_tier_list("info") == ["fmp", "finnhub", "alpha_vantage", "yfinance"]
        assert config.get_tier_list("insider_trades") == ["fmp", "finnhub"]
        # Type B
        assert config.get_tier_list("quote") == ["fmp", "finnhub", "yfinance", "alpha_vantage"]
        # Type C
        assert config.get_tier_list("ratios", fresh=False) == ["fmp", "finnhub"]
        assert config.get_tier_list("ratios", fresh=True) == ["fmp", "finnhub"]
        assert config.get_tier_list("earnings", fresh=False) == [
            "fmp",
            "finnhub",
            "alpha_vantage",
        ]
        assert config.get_tier_list("earnings", fresh=True) == ["fmp", "finnhub"]

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
            "fmp",
            "finnhub",
            "twelve_data",
            "yfinance",
            "alpha_vantage",
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
