"""Unit tests for YFinanceProvider with mocked yfinance responses.

These tests mock ``yf.Ticker`` so they don't hit the network.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from onefinance.core.errors import ProviderError
from onefinance.core.models import CompanyInfo, PriceBar
from onefinance.providers.yfinance_provider import YFinanceProvider


@pytest.fixture
def provider() -> YFinanceProvider:
    return YFinanceProvider(timeout=5)


# -----------------------------------------------------------------------
# get_price_history
# -----------------------------------------------------------------------

class TestGetPriceHistory:
    def test_returns_price_bars(self, provider: YFinanceProvider):
        """Normal case: yfinance returns a DataFrame with OHLCV data."""
        index = pd.DatetimeIndex(
            [datetime(2024, 1, 2), datetime(2024, 1, 3)],
            name="Date",
        )
        df = pd.DataFrame(
            {
                "Open": [185.0, 186.0],
                "High": [186.5, 187.0],
                "Low": [184.0, 185.5],
                "Close": [185.64, 186.80],
                "Volume": [50_000_000, 45_000_000],
            },
            index=index,
        )

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 4))

        assert len(bars) == 2
        assert all(isinstance(b, PriceBar) for b in bars)
        assert bars[0].symbol == "AAPL"
        assert bars[0].open == 185.0
        assert bars[0].close == 185.64
        assert bars[0].volume == 50_000_000
        assert bars[0].source == "yfinance"
        assert bars[0].fetched_at.tzinfo is not None  # UTC

    def test_empty_dataframe_returns_empty_list(self, provider: YFinanceProvider):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            bars = provider.get_price_history("INVALID", date(2024, 1, 1), date(2024, 1, 2))

        assert bars == []

    def test_none_dataframe_returns_empty_list(self, provider: YFinanceProvider):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = None

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            bars = provider.get_price_history("INVALID", date(2024, 1, 1), date(2024, 1, 2))

        assert bars == []

    def test_network_error_raises_provider_error(self, provider: YFinanceProvider):
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = ConnectionError("network down")

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 1, 2))
            assert exc_info.value.code == "NETWORK_ERROR"
            assert exc_info.value.retry_safe is True

    def test_adj_close_fallback_to_close(self, provider: YFinanceProvider):
        """When 'Adj Close' is missing, fall back to 'Close'."""
        index = pd.DatetimeIndex([datetime(2024, 1, 2)], name="Date")
        df = pd.DataFrame(
            {
                "Open": [185.0],
                "High": [186.5],
                "Low": [184.0],
                "Close": [185.64],
                "Volume": [50_000_000],
                # No "Adj Close" column
            },
            index=index,
        )

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))

        assert len(bars) == 1
        assert bars[0].adj_close == 185.64  # fell back to Close

    def test_symbol_uppercased(self, provider: YFinanceProvider):
        index = pd.DatetimeIndex([datetime(2024, 1, 2)], name="Date")
        df = pd.DataFrame(
            {
                "Open": [185.0],
                "High": [186.5],
                "Low": [184.0],
                "Close": [185.64],
                "Volume": [50_000_000],
            },
            index=index,
        )

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))

        assert bars[0].symbol == "AAPL"


# -----------------------------------------------------------------------
# get_info
# -----------------------------------------------------------------------

class TestGetInfo:
    def test_returns_company_info(self, provider: YFinanceProvider):
        mock_info = {
            "quoteType": "EQUITY",
            "longName": "Apple Inc.",
            "exchange": "NMS",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "country": "United States",
            "marketCap": 3_000_000_000_000,
            "longBusinessSummary": "Apple designs, manufactures...",
            "website": "https://apple.com",
            "fullTimeEmployees": 164_000,
            "currency": "USD",
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            info = provider.get_info("AAPL")

        assert isinstance(info, CompanyInfo)
        assert info.symbol == "AAPL"
        assert info.name == "Apple Inc."
        assert info.sector == "Technology"
        assert info.market_cap == 3_000_000_000_000.0
        assert info.employees == 164_000
        assert info.currency == "USD"
        assert info.source == "yfinance"

    def test_minimal_info(self, provider: YFinanceProvider):
        """yfinance sometimes returns very sparse info dicts."""
        mock_info = {
            "quoteType": "EQUITY",
            "shortName": "Apple",
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            info = provider.get_info("AAPL")

        assert info.name == "Apple"
        assert info.sector is None
        assert info.market_cap is None

    def test_empty_info_raises(self, provider: YFinanceProvider):
        mock_ticker = MagicMock()
        mock_ticker.info = {}

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_info("INVALID")
            assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_none_info_raises(self, provider: YFinanceProvider):
        mock_ticker = MagicMock()
        mock_ticker.info = None

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_info("INVALID")
            assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_network_error(self, provider: YFinanceProvider):
        mock_ticker = MagicMock()
        type(mock_ticker).info = property(lambda self: (_ for _ in ()).throw(ConnectionError("fail")))

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_info("AAPL")
            assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# Rate-limit detection
# -----------------------------------------------------------------------

class TestRateLimitDetection:
    def test_none_response_is_rate_limited(self, provider: YFinanceProvider):
        assert provider.is_rate_limited(None) is True

    def test_normal_response_not_rate_limited(self, provider: YFinanceProvider):
        assert provider.is_rate_limited("some data") is False

    def test_rate_limit_exception(self, provider: YFinanceProvider):
        exc = Exception("Too many requests, rate limited")
        assert provider.is_rate_limited(exc) is True

    def test_429_exception(self, provider: YFinanceProvider):
        exc = Exception("HTTP 429 error")
        assert provider.is_rate_limited(exc) is True

    def test_cooldown_is_5_minutes(self, provider: YFinanceProvider):
        assert provider.cooldown_for(None) == 300.0


# -----------------------------------------------------------------------
# Capability discovery
# -----------------------------------------------------------------------

class TestYFinanceCapabilities:
    def test_supports_price_history(self, provider: YFinanceProvider):
        assert provider.supports("price_history") is True

    def test_supports_info(self, provider: YFinanceProvider):
        assert provider.supports("info") is True

    def test_does_not_support_ratios(self, provider: YFinanceProvider):
        assert provider.supports("ratios") is False

    def test_does_not_support_earnings(self, provider: YFinanceProvider):
        assert provider.supports("earnings") is False

    def test_does_not_support_insider_trades(self, provider: YFinanceProvider):
        assert provider.supports("insider_trades") is False

    def test_supported_endpoints(self, provider: YFinanceProvider):
        endpoints = provider.supported_endpoints
        assert "price_history" in endpoints
        assert "info" in endpoints
        assert "ratios" not in endpoints
