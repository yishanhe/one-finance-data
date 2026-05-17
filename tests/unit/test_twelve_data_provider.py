"""Unit tests for TwelveDataProvider with mocked httpx responses."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from onefinance.core.errors import ConfigError, ProviderError, RateLimitError
from onefinance.core.models import PriceBar, Quote
from onefinance.providers.twelve_data import TwelveDataProvider


@pytest.fixture
def provider() -> TwelveDataProvider:
    return TwelveDataProvider(api_key="test_key", timeout=5)


def _mock_response(data: Any, status_code: int = 200) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data) if not isinstance(data, str) else data
    return resp


# -----------------------------------------------------------------------
# Constructor
# -----------------------------------------------------------------------


class TestConstructor:
    def test_api_key_from_param(self) -> None:
        p = TwelveDataProvider(api_key="my_key")
        assert p._api_key == "my_key"

    def test_api_key_from_env(self) -> None:
        with patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "env_key"}):
            p = TwelveDataProvider()
            assert p._api_key == "env_key"

    def test_missing_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ConfigError):
                TwelveDataProvider()

    def test_name(self) -> None:
        assert TwelveDataProvider(api_key="k").name == "twelve_data"


# -----------------------------------------------------------------------
# Rate-limit detection
# -----------------------------------------------------------------------


class TestRateLimitDetection:
    def test_http_429_raises(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 429)):
            with pytest.raises(RateLimitError):
                provider._get("time_series", {})

    def test_body_429_code_raises(self, provider: TwelveDataProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({"code": 429, "message": "Too Many Requests"}),
        ):
            with pytest.raises(RateLimitError):
                provider._get("time_series", {})

    def test_body_error_status_raises_provider_error(self, provider: TwelveDataProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({"status": "error", "message": "Invalid symbol"}),
        ):
            with pytest.raises(ProviderError):
                provider._get("time_series", {})


# -----------------------------------------------------------------------
# get_price_history
# -----------------------------------------------------------------------


class TestGetPriceHistory:
    _ts_data = {
        "meta": {"symbol": "AAPL", "interval": "1day"},
        "values": [
            {
                "datetime": "2024-01-02",
                "open": "184.22",
                "high": "185.88",
                "low": "183.43",
                "close": "184.25",
                "volume": "58414500",
            },
            {
                "datetime": "2024-01-03",
                "open": "185.00",
                "high": "186.10",
                "low": "184.50",
                "close": "185.64",
                "volume": "52000000",
            },
        ],
        "status": "ok",
    }

    def test_returns_bars(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._ts_data)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))
        assert len(bars) == 2
        assert all(isinstance(b, PriceBar) for b in bars)
        assert bars[0].close == 184.25
        assert bars[0].source == "twelve_data"

    def test_empty_values_returns_empty(self, provider: TwelveDataProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response({"values": [], "status": "ok"})
        ):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))
        assert bars == []

    def test_symbol_uppercased(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._ts_data)):
            bars = provider.get_price_history("aapl", date(2024, 1, 2), date(2024, 1, 3))
        assert bars[0].symbol == "AAPL"

    def test_volume_as_int(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._ts_data)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))
        assert bars[0].volume == 58414500
        assert isinstance(bars[0].volume, int)


# -----------------------------------------------------------------------
# get_quote
# -----------------------------------------------------------------------


class TestGetQuote:
    _quote_data = {
        "symbol": "AAPL",
        "name": "Apple Inc",
        "exchange": "NASDAQ",
        "currency": "USD",
        "datetime": "2024-01-02",
        "timestamp": 1704204000,
        "open": "184.22",
        "high": "186.00",
        "low": "184.00",
        "close": "185.64",
        "volume": "52000000",
        "previous_close": "184.00",
        "change": "1.64",
        "percent_change": "0.89",
        "is_market_open": False,
    }

    def test_returns_quote(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._quote_data)):
            q = provider.get_quote("AAPL")
        assert isinstance(q, Quote)
        assert q.symbol == "AAPL"
        assert q.price == 185.64
        assert q.volume == 52000000
        assert q.source == "twelve_data"

    def test_symbol_uppercased(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._quote_data)):
            q = provider.get_quote("aapl")
        assert q.symbol == "AAPL"

    def test_error_status_raises(self, provider: TwelveDataProvider) -> None:
        # _get() intercepts status=error and raises PROVIDER_ERROR before get_quote sees it
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({"status": "error", "message": "Symbol not found"}),
        ):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("FAKE")
        assert exc_info.value.code == "PROVIDER_ERROR"

    def test_missing_close_raises(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({})):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("AAPL")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"
