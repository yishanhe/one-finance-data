"""Unit tests for the provider registry and factory."""

from __future__ import annotations

from unittest.mock import patch

from onefinance.core.client import _providers_from_config
from onefinance.core.config import OneFinanceConfig, ProviderConfig, _default_config
from onefinance.providers import _factory
from onefinance.providers._factory import ProviderSpec, build, iter_specs, register
from onefinance.providers.base import BaseProvider


class TestBuild:
    def test_fmp_with_key(self) -> None:
        cfg = ProviderConfig(name="fmp", api_key_env="FMP_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {"FMP_API_KEY": "test_key"}):
            p = build("fmp", cfg)
        assert p is not None
        assert p.name == "fmp"

    def test_fmp_without_key_returns_none(self) -> None:
        cfg = ProviderConfig(name="fmp", api_key_env="FMP_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {}, clear=True):
            p = build("fmp", cfg)
        assert p is None

    def test_finnhub_with_key(self) -> None:
        cfg = ProviderConfig(name="finnhub", api_key_env="FINNHUB_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {"FINNHUB_API_KEY": "test_key"}):
            p = build("finnhub", cfg)
        assert p is not None
        assert p.name == "finnhub"

    def test_finnhub_without_key_returns_none(self) -> None:
        cfg = ProviderConfig(name="finnhub", api_key_env="FINNHUB_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {}, clear=True):
            p = build("finnhub", cfg)
        assert p is None

    def test_twelve_data_with_key(self) -> None:
        cfg = ProviderConfig(name="twelve_data", api_key_env="TWELVE_DATA_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "test_key"}):
            p = build("twelve_data", cfg)
        assert p is not None
        assert p.name == "twelve_data"

    def test_twelve_data_without_key_returns_none(self) -> None:
        cfg = ProviderConfig(name="twelve_data", api_key_env="TWELVE_DATA_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {}, clear=True):
            p = build("twelve_data", cfg)
        assert p is None

    def test_yfinance_no_key_required(self) -> None:
        cfg = ProviderConfig(name="yfinance", timeout_s=15)
        p = build("yfinance", cfg)
        assert p is not None
        assert p.name == "yfinance"

    def test_alpha_vantage_with_key(self) -> None:
        cfg = ProviderConfig(name="alpha_vantage", api_key_env="ALPHAVANTAGE_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {"ALPHAVANTAGE_API_KEY": "test_key"}):
            p = build("alpha_vantage", cfg)
        assert p is not None
        assert p.name == "alpha_vantage"

    def test_alpha_vantage_without_key_returns_none(self) -> None:
        cfg = ProviderConfig(name="alpha_vantage", api_key_env="ALPHAVANTAGE_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {}, clear=True):
            assert build("alpha_vantage", cfg) is None

    def test_polygon_with_key(self) -> None:
        cfg = ProviderConfig(name="polygon", api_key_env="POLYGON_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {"POLYGON_API_KEY": "test_key"}):
            p = build("polygon", cfg)
        assert p is not None
        assert p.name == "polygon"

    def test_polygon_without_key_returns_none(self) -> None:
        cfg = ProviderConfig(name="polygon", api_key_env="POLYGON_API_KEY", timeout_s=10)
        with patch.dict("os.environ", {}, clear=True):
            assert build("polygon", cfg) is None

    def test_unknown_provider_returns_none(self) -> None:
        cfg = ProviderConfig(name="unknown")
        p = build("unknown", cfg)
        assert p is None


class TestRegistry:
    def test_builtins_registered(self) -> None:
        names = {spec.name for spec in iter_specs()}
        assert {"fmp", "finnhub", "twelve_data", "yfinance", "alpha_vantage", "polygon"}.issubset(
            names
        )

    def test_register_makes_spec_visible(self) -> None:
        from typing import Any

        class _FakeProvider(BaseProvider):
            name = "_fake"

            def is_rate_limited(self, response: Any) -> bool:
                return False

            def cooldown_for(self, response: Any) -> float:
                return 0.0

        def _builder(cfg: ProviderConfig, http_client: Any = None) -> BaseProvider | None:
            return _FakeProvider()

        spec = ProviderSpec("_fake", _builder, requires_api_key=False)
        register(spec)
        try:
            assert spec in list(iter_specs())
            built = build("_fake", ProviderConfig(name="_fake"))
            assert built is not None
            assert built.name == "_fake"
        finally:
            _factory._REGISTRY.pop("_fake", None)


class TestProvidersFromConfig:
    def test_all_keys_set_returns_all_providers(self) -> None:
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

    def test_no_keys_set_falls_back_to_yfinance(self) -> None:
        cfg = _default_config()
        with patch.dict("os.environ", {}, clear=True):
            providers = _providers_from_config(cfg)
        assert len(providers) >= 1
        names = {p.name for p in providers}
        assert "yfinance" in names

    def test_partial_keys_returns_available_providers(self) -> None:
        cfg = _default_config()
        with patch.dict("os.environ", {"FMP_API_KEY": "fmp_k"}, clear=True):
            providers = _providers_from_config(cfg)
        names = {p.name for p in providers}
        assert "fmp" in names
        assert "yfinance" in names
        assert "finnhub" not in names
        assert "twelve_data" not in names

    def test_empty_config_returns_yfinance(self) -> None:
        cfg = OneFinanceConfig(providers={})
        providers = _providers_from_config(cfg)
        assert len(providers) == 1
        assert providers[0].name == "yfinance"

    def test_provider_order_preserved(self) -> None:
        cfg = _default_config()
        env = {
            "FMP_API_KEY": "fmp_k",
            "FINNHUB_API_KEY": "fh_k",
        }
        with patch.dict("os.environ", env, clear=True):
            providers = _providers_from_config(cfg)
        names = [p.name for p in providers if p.name in {"fmp", "finnhub"}]
        assert names.index("fmp") < names.index("finnhub")


class TestUnconfiguredProviderNeverTried:
    """Guarantee: missing API key → provider not built → never dispatched."""

    def test_missing_api_key_excluded_from_router(self) -> None:
        """With no API keys set, only yfinance is built.
        The tier list for 'quote' names fmp/finnhub/polygon/alpha_vantage/yfinance,
        but the router must only resolve yfinance.
        """
        from onefinance.core.router import ProviderRouter

        cfg = _default_config()
        with patch.dict("os.environ", {}, clear=True):
            providers = _providers_from_config(cfg)

        provider_map = {p.name: p for p in providers}
        assert "fmp" not in provider_map
        assert "finnhub" not in provider_map
        assert "yfinance" in provider_map

        router = ProviderRouter(provider_map, cfg)
        selected = router._select_providers("quote")
        names = [p.name for p in selected]
        # Only yfinance is registered; all others are silently absent
        assert names == ["yfinance"]

    def test_unconfigured_provider_in_fallback_order_is_skipped(self) -> None:
        """Provider name in fallback_order but not built → never added to dispatch list."""
        from onefinance.core.router import ProviderRouter
        from onefinance.providers.yfinance_provider import YFinanceProvider

        cfg = _default_config()
        cfg.fallback_order = ["fmp", "yfinance"]  # fmp not registered

        provider_map: dict[str, BaseProvider] = {"yfinance": YFinanceProvider()}
        router = ProviderRouter(provider_map, cfg)

        # ratios tier: ["fmp", "finnhub", "yfinance"] — fmp/finnhub not in map
        selected = router._select_providers("ratios")
        names = [p.name for p in selected]
        assert names == ["yfinance"]  # fmp silently absent despite being in fallback_order
