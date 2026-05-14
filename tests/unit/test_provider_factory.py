"""Unit tests for _providers_from_config and _instantiate_provider."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from onefinance.core.client import _instantiate_provider, _providers_from_config
from onefinance.core.config import OneFinanceConfig, ProviderConfig, _default_config


class TestInstantiateProvider:
    def test_fmp_with_key(self):
        cfg = ProviderConfig(name="fmp", api_key_env="FMP_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {"FMP_API_KEY": "test_key"}):
            p = _instantiate_provider("fmp", cfg)
        assert p is not None
        assert p.name == "fmp"

    def test_fmp_without_key_returns_none(self):
        cfg = ProviderConfig(name="fmp", api_key_env="FMP_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {}, clear=True):
            p = _instantiate_provider("fmp", cfg)
        assert p is None

    def test_finnhub_with_key(self):
        cfg = ProviderConfig(name="finnhub", api_key_env="FINNHUB_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {"FINNHUB_API_KEY": "test_key"}):
            p = _instantiate_provider("finnhub", cfg)
        assert p is not None
        assert p.name == "finnhub"

    def test_finnhub_without_key_returns_none(self):
        cfg = ProviderConfig(name="finnhub", api_key_env="FINNHUB_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {}, clear=True):
            p = _instantiate_provider("finnhub", cfg)
        assert p is None

    def test_twelve_data_with_key(self):
        cfg = ProviderConfig(name="twelve_data", api_key_env="TWELVE_DATA_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "test_key"}):
            p = _instantiate_provider("twelve_data", cfg)
        assert p is not None
        assert p.name == "twelve_data"

    def test_twelve_data_without_key_returns_none(self):
        cfg = ProviderConfig(name="twelve_data", api_key_env="TWELVE_DATA_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {}, clear=True):
            p = _instantiate_provider("twelve_data", cfg)
        assert p is None

    def test_yfinance_no_key_required(self):
        cfg = ProviderConfig(name="yfinance", timeout_s=15)
        p = _instantiate_provider("yfinance", cfg)
        assert p is not None
        assert p.name == "yfinance"

    def test_unknown_provider_returns_none(self):
        cfg = ProviderConfig(name="unknown")
        p = _instantiate_provider("unknown", cfg)
        assert p is None


class TestProvidersFromConfig:
    def test_all_keys_set_returns_all_providers(self):
        cfg = _default_config()
        env = {
            "FMP_API_KEY": "fmp_k",
            "FINNHUB_API_KEY": "fh_k",
            "TWELVE_DATA_API_KEY": "td_k",
        }
        with patch.dict("os.environ", env):
            providers = _providers_from_config(cfg)
        names = {p.name for p in providers}
        assert "fmp" in names
        assert "finnhub" in names
        assert "twelve_data" in names
        assert "yfinance" in names

    def test_no_keys_set_falls_back_to_yfinance(self):
        cfg = _default_config()
        with patch.dict("os.environ", {}, clear=True):
            providers = _providers_from_config(cfg)
        assert len(providers) >= 1
        names = {p.name for p in providers}
        assert "yfinance" in names

    def test_partial_keys_returns_available_providers(self):
        cfg = _default_config()
        with patch.dict("os.environ", {"FMP_API_KEY": "fmp_k"}, clear=True):
            providers = _providers_from_config(cfg)
        names = {p.name for p in providers}
        assert "fmp" in names
        assert "yfinance" in names
        assert "finnhub" not in names
        assert "twelve_data" not in names

    def test_empty_config_returns_yfinance(self):
        cfg = OneFinanceConfig(providers={})
        providers = _providers_from_config(cfg)
        assert len(providers) == 1
        assert providers[0].name == "yfinance"

    def test_provider_order_preserved(self):
        cfg = _default_config()
        env = {
            "FMP_API_KEY": "fmp_k",
            "FINNHUB_API_KEY": "fh_k",
        }
        with patch.dict("os.environ", env, clear=True):
            providers = _providers_from_config(cfg)
        names = [p.name for p in providers if p.name in {"fmp", "finnhub"}]
        assert names.index("fmp") < names.index("finnhub")
