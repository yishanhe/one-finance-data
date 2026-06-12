"""Unit tests for YFinanceProvider.get_short_interest."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from onefinance.core.models import ShortInterest
from onefinance.providers.yfinance_provider import YFinanceProvider


@pytest.fixture
def provider() -> YFinanceProvider:
    return YFinanceProvider()


class TestGetShortInterest:
    def test_returns_short_interest(self, provider: YFinanceProvider) -> None:
        mock_info = {
            "sharesShort": 50_000_000,
            "shortRatio": 2.5,
            "shortPercentOfFloat": 0.035,
            "dateShortInterest": 1717200000,
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = provider.get_short_interest("AAPL")

        assert isinstance(result, ShortInterest)
        assert result.symbol == "AAPL"
        assert result.short_interest == 50_000_000
        assert result.days_to_cover == 2.5
        assert result.short_float_pct is not None
        assert abs(result.short_float_pct - 3.5) < 0.01
        assert result.settlement_date is not None
        assert result.source == "yfinance"

    def test_partial_data_returns_nones(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = {}

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = provider.get_short_interest("AAPL")

        assert result.short_interest is None
        assert result.days_to_cover is None
        assert result.short_float_pct is None

    def test_network_error_raises_provider_error(self, provider: YFinanceProvider) -> None:
        from onefinance.core.errors import ProviderError

        mock_ticker = MagicMock()
        type(mock_ticker).info = property(
            lambda self: (_ for _ in ()).throw(Exception("network timeout"))
        )

        with patch("yfinance.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError):
                provider.get_short_interest("AAPL")

    def test_symbol_normalised_uppercase(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = {}

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = provider.get_short_interest("aapl")

        assert result.symbol == "AAPL"
