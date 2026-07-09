"""Unit tests for YFinanceProvider with mocked yfinance responses.

These tests mock ``yf.Ticker`` so they don't hit the network.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, PropertyMock, patch

import pandas as pd  # type: ignore[import-untyped]
import pytest

from onefinance.core.errors import ProviderError
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    IncomeStatement,
    InstitutionalHolder,
    NewsArticle,
    OptionChain,
    PriceBar,
    Quote,
    SectorInfo,
)
from onefinance.providers.yfinance_provider import YFinanceProvider


@pytest.fixture
def provider() -> YFinanceProvider:
    return YFinanceProvider(timeout=5)


# -----------------------------------------------------------------------
# get_price_history
# -----------------------------------------------------------------------


class TestGetPriceHistory:
    def test_returns_price_bars(self, provider: YFinanceProvider) -> None:
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

    def test_history_requests_unadjusted_close(self, provider: YFinanceProvider) -> None:
        """Keep Close comparable to live quote price; expose adjusted price separately."""
        index = pd.DatetimeIndex([datetime(2024, 1, 2)], name="Date")
        df = pd.DataFrame(
            {
                "Open": [1200.0],
                "High": [1225.0],
                "Low": [1190.0],
                "Close": [1210.0],
                "Adj Close": [1080.0],
                "Volume": [1_000_000],
            },
            index=index,
        )

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            bars = provider.get_price_history("285A.T", date(2024, 1, 2), date(2024, 1, 3))

        mock_ticker.history.assert_called_once()
        assert mock_ticker.history.call_args.kwargs["auto_adjust"] is False
        assert bars[0].close == 1210.0
        assert bars[0].adj_close == 1080.0

    def test_empty_dataframe_returns_empty_list(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            bars = provider.get_price_history("INVALID", date(2024, 1, 1), date(2024, 1, 2))

        assert bars == []

    def test_none_dataframe_returns_empty_list(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = None

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            bars = provider.get_price_history("INVALID", date(2024, 1, 1), date(2024, 1, 2))

        assert bars == []

    def test_network_error_raises_provider_error(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.side_effect = ConnectionError("network down")

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 1, 2))
            assert exc_info.value.code == "NETWORK_ERROR"
            assert exc_info.value.retry_safe is True

    def test_adj_close_fallback_to_close(self, provider: YFinanceProvider) -> None:
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

    def test_symbol_uppercased(self, provider: YFinanceProvider) -> None:
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
    def test_returns_company_info(self, provider: YFinanceProvider) -> None:
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

    def test_minimal_info(self, provider: YFinanceProvider) -> None:
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

    def test_empty_info_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = {}

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_info("INVALID")
            assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_none_info_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = None

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_info("INVALID")
            assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_network_error(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).info = property(
            lambda self: (_ for _ in ()).throw(ConnectionError("fail"))
        )

        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_info("AAPL")
            assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# Rate-limit detection
# -----------------------------------------------------------------------


class TestRateLimitDetection:
    def test_none_response_is_rate_limited(self, provider: YFinanceProvider) -> None:
        assert provider.is_rate_limited(None) is True

    def test_normal_response_not_rate_limited(self, provider: YFinanceProvider) -> None:
        assert provider.is_rate_limited("some data") is False

    def test_rate_limit_exception(self, provider: YFinanceProvider) -> None:
        exc = Exception("Too many requests, rate limited")
        assert provider.is_rate_limited(exc) is True

    def test_429_exception(self, provider: YFinanceProvider) -> None:
        exc = Exception("HTTP 429 error")
        assert provider.is_rate_limited(exc) is True

    def test_cooldown_is_5_minutes(self, provider: YFinanceProvider) -> None:
        assert provider.cooldown_for(None) == 300.0


# -----------------------------------------------------------------------
# Capability discovery
# -----------------------------------------------------------------------


class TestYFinanceCapabilities:
    def test_supports_price_history(self, provider: YFinanceProvider) -> None:
        assert provider.supports("price_history") is True

    def test_supports_info(self, provider: YFinanceProvider) -> None:
        assert provider.supports("info") is True

    def test_supports_ratios(self, provider: YFinanceProvider) -> None:
        assert provider.supports("ratios") is True

    def test_supports_earnings(self, provider: YFinanceProvider) -> None:
        assert provider.supports("earnings") is True

    def test_supports_insider_trades(self, provider: YFinanceProvider) -> None:
        assert provider.supports("insider_trades") is True

    def test_supported_endpoints(self, provider: YFinanceProvider) -> None:
        endpoints = provider.supported_endpoints
        assert "price_history" in endpoints
        assert "info" in endpoints
        assert "ratios" in endpoints
        assert "earnings" in endpoints
        assert "insider_trades" in endpoints


# -----------------------------------------------------------------------
# get_price_history — bad bar skipping
# -----------------------------------------------------------------------


class TestGetPriceHistoryBadBar:
    def test_skips_bad_bar_continues(self, provider: YFinanceProvider) -> None:
        index = pd.DatetimeIndex([datetime(2024, 1, 2), datetime(2024, 1, 3)], name="Date")
        df = pd.DataFrame(
            {
                "Open": [185.0, None],
                "High": [186.5, None],
                "Low": [184.0, None],
                "Close": [185.64, "bad"],
                "Volume": [50_000_000, "bad"],
            },
            index=index,
        )
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 4))
        assert len(bars) == 1
        assert bars[0].close == 185.64


# -----------------------------------------------------------------------
# get_quote
# -----------------------------------------------------------------------


class TestGetQuote:
    def test_returns_quote(self, provider: YFinanceProvider) -> None:
        mock_info = {
            "quoteType": "EQUITY",
            "currentPrice": 185.64,
            "bid": 185.60,
            "ask": 185.68,
            "volume": 52_000_000,
            "marketCap": 2_900_000_000_000,
            "previousClose": 183.00,
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            q = provider.get_quote("AAPL")
        assert isinstance(q, Quote)
        assert q.symbol == "AAPL"
        assert q.price == 185.64
        assert q.bid == 185.60
        assert q.ask == 185.68
        assert q.volume == 52_000_000
        assert q.market_cap == 2_900_000_000_000.0
        assert q.source == "yfinance"
        assert q.prev_close == 183.00
        assert q.change_pct == pytest.approx((185.64 - 183.00) / 183.00 * 100, rel=1e-4)

    def test_falls_back_to_regular_market_previous_close(self, provider: YFinanceProvider) -> None:
        mock_info = {
            "quoteType": "EQUITY",
            "regularMarketPrice": 184.00,
            "regularMarketVolume": 40_000_000,
            "regularMarketPreviousClose": 180.00,
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            q = provider.get_quote("AAPL")
        assert q.prev_close == 180.00

    def test_falls_back_to_regular_market_price(self, provider: YFinanceProvider) -> None:
        mock_info = {
            "quoteType": "EQUITY",
            "regularMarketPrice": 184.00,
            "regularMarketVolume": 40_000_000,
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            q = provider.get_quote("AAPL")
        assert q.price == 184.00

    def test_empty_info_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("INVALID")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_none_info_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = None
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("INVALID")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).info = PropertyMock(side_effect=ConnectionError("network fail"))
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("AAPL")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_news
# -----------------------------------------------------------------------


class TestGetNews:
    _news_data = [
        {
            "title": "Apple reports record earnings",
            "publisher": "Reuters",
            "link": "https://reuters.com/apple",
            "providerPublishTime": 1704196800,
            "summary": "Apple beat estimates...",
        },
        {
            "title": "Apple launches new product",
            "publisher": "Bloomberg",
            "link": "https://bloomberg.com/apple",
            "providerPublishTime": 1704110400,
        },
    ]

    def test_returns_articles(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.news = self._news_data
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            articles = provider.get_news("AAPL")
        assert len(articles) == 2
        assert all(isinstance(a, NewsArticle) for a in articles)
        assert articles[0].title == "Apple reports record earnings"
        assert articles[0].publisher == "Reuters"
        assert articles[0].source == "yfinance"

    def test_empty_news_returns_empty(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.news = []
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            articles = provider.get_news("AAPL")
        assert articles == []

    def test_limit_respected(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.news = self._news_data * 5
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            articles = provider.get_news("AAPL", limit=3)
        assert len(articles) == 3

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).news = PropertyMock(side_effect=ConnectionError("fail"))
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_news("AAPL")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_corporate_actions
# -----------------------------------------------------------------------


class TestGetCorporateActions:
    def test_returns_dividends_and_splits(self, provider: YFinanceProvider) -> None:
        div_index = pd.DatetimeIndex([datetime(2024, 1, 15)], name="Date")
        divs = pd.Series([0.24], index=div_index)

        split_index = pd.DatetimeIndex([datetime(2023, 8, 25)], name="Date")
        splits = pd.Series([4.0], index=split_index)

        mock_ticker = MagicMock()
        mock_ticker.dividends = divs
        mock_ticker.splits = splits
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            actions = provider.get_corporate_actions("AAPL")
        assert len(actions) == 2
        types = {a.action_type for a in actions}
        assert "dividend" in types
        assert "split" in types

    def test_empty_dividends_and_splits(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.dividends = pd.Series([], dtype=float)
        mock_ticker.splits = pd.Series([], dtype=float)
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            actions = provider.get_corporate_actions("AAPL")
        assert actions == []

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).dividends = PropertyMock(side_effect=ConnectionError("fail"))
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_corporate_actions("AAPL")
        assert exc_info.value.code == "NETWORK_ERROR"

    def test_sorted_descending(self, provider: YFinanceProvider) -> None:
        div_index = pd.DatetimeIndex([datetime(2024, 1, 15), datetime(2023, 10, 15)], name="Date")
        divs = pd.Series([0.24, 0.23], index=div_index)
        mock_ticker = MagicMock()
        mock_ticker.dividends = divs
        mock_ticker.splits = pd.Series([], dtype=float)
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            actions = provider.get_corporate_actions("AAPL")
        assert actions[0].date >= actions[1].date


# -----------------------------------------------------------------------
# get_institutional_holders
# -----------------------------------------------------------------------


class TestGetInstitutionalHolders:
    def test_returns_holders(self, provider: YFinanceProvider) -> None:
        df = pd.DataFrame(
            {
                "Holder": ["Vanguard Group", "BlackRock"],
                "Shares": [1_200_000_000, 1_100_000_000],
                "Value": [222_000_000_000, 203_000_000_000],
                "% Out": [7.8, 7.1],
            }
        )
        mock_ticker = MagicMock()
        mock_ticker.institutional_holders = df
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            holders = provider.get_institutional_holders("AAPL")
        assert len(holders) == 2
        assert all(isinstance(h, InstitutionalHolder) for h in holders)
        assert holders[0].holder_name == "Vanguard Group"
        assert holders[0].source == "yfinance"

    def test_empty_dataframe_returns_empty(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.institutional_holders = pd.DataFrame()
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            holders = provider.get_institutional_holders("AAPL")
        assert holders == []

    def test_none_dataframe_returns_empty(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.institutional_holders = None
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            holders = provider.get_institutional_holders("AAPL")
        assert holders == []

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).institutional_holders = PropertyMock(side_effect=ConnectionError("fail"))
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_institutional_holders("AAPL")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_analyst_data
# -----------------------------------------------------------------------


class TestGetAnalystData:
    def test_returns_analyst_data(self, provider: YFinanceProvider) -> None:
        mock_info = {
            "targetHighPrice": 220.0,
            "targetLowPrice": 160.0,
            "targetMeanPrice": 195.0,
            "targetMedianPrice": 197.0,
            "numberOfAnalystOpinions": 35,
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            data = provider.get_analyst_data("AAPL")
        assert isinstance(data, AnalystData)
        assert data.target_high == 220.0
        assert data.target_low == 160.0
        assert data.target_mean == 195.0
        assert data.source == "yfinance"

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).info = PropertyMock(side_effect=ConnectionError("fail"))
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_analyst_data("AAPL")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_options_expirations
# -----------------------------------------------------------------------


class TestGetOptionsExpirations:
    def test_returns_dates(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.options = ("2024-01-19", "2024-02-16", "2024-03-15")
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            dates = provider.get_options_expirations("AAPL")
        assert len(dates) == 3
        assert all(isinstance(d, date) for d in dates)
        assert dates[0] == date(2024, 1, 19)

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).options = PropertyMock(side_effect=ConnectionError("fail"))
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_options_expirations("AAPL")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_option_chain
# -----------------------------------------------------------------------


class TestGetOptionChain:
    def test_returns_option_chain(self, provider: YFinanceProvider) -> None:
        calls_df = pd.DataFrame(
            {
                "contractSymbol": ["AAPL240119C00190000"],
                "strike": [190.0],
                "lastPrice": [5.20],
                "bid": [5.10],
                "ask": [5.30],
                "volume": [1000],
                "openInterest": [5000],
                "impliedVolatility": [0.25],
                "inTheMoney": [False],
            }
        )
        puts_df = pd.DataFrame(
            {
                "contractSymbol": ["AAPL240119P00190000"],
                "strike": [190.0],
                "lastPrice": [3.80],
                "bid": [3.70],
                "ask": [3.90],
                "volume": [800],
                "openInterest": [3000],
                "impliedVolatility": [0.27],
                "inTheMoney": [True],
            }
        )
        mock_chain = MagicMock()
        mock_chain.calls = calls_df
        mock_chain.puts = puts_df

        mock_ticker = MagicMock()
        mock_ticker.option_chain.return_value = mock_chain
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            chain = provider.get_option_chain("AAPL", date(2024, 1, 19))
        assert isinstance(chain, OptionChain)
        assert len(chain.calls) == 1
        assert len(chain.puts) == 1
        assert chain.calls[0].strike == 190.0
        assert chain.source == "yfinance"

    def test_empty_chain_raises(self, provider: YFinanceProvider) -> None:
        # yfinance returns empty DataFrames on weekends / unavailable symbols.
        # Provider must raise ProviderError so the router falls through to the next provider.
        mock_chain = MagicMock()
        mock_chain.calls = pd.DataFrame()
        mock_chain.puts = pd.DataFrame()
        mock_ticker = MagicMock()
        mock_ticker.option_chain.return_value = mock_chain
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_option_chain("AAPL", date(2024, 1, 19))
        assert exc_info.value.code == "EMPTY_RESPONSE"

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.option_chain.side_effect = ConnectionError("fail")
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_option_chain("AAPL", date(2024, 1, 19))
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_sector_overview
# -----------------------------------------------------------------------


class TestGetSectorOverview:
    def test_returns_sector_info(self, provider: YFinanceProvider) -> None:
        mock_sector = MagicMock()
        mock_sector.overview = {"market_weight": 0.285}
        top_df = pd.DataFrame({"marketCap": [3e12, 2.8e12, 2e12]}, index=["AAPL", "MSFT", "NVDA"])
        mock_sector.top_companies = top_df

        with patch("onefinance.providers.yfinance_provider.yf.Sector", return_value=mock_sector):
            info = provider.get_sector_overview("technology")
        assert isinstance(info, SectorInfo)
        assert info.name == "Technology"
        assert info.market_weight == 0.285
        assert info.top_companies is not None and "AAPL" in info.top_companies
        assert info.source == "yfinance"

    def test_empty_top_companies(self, provider: YFinanceProvider) -> None:
        mock_sector = MagicMock()
        mock_sector.overview = {}
        mock_sector.top_companies = pd.DataFrame()
        with patch("onefinance.providers.yfinance_provider.yf.Sector", return_value=mock_sector):
            info = provider.get_sector_overview("technology")
        assert info.top_companies == []

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        with patch(
            "onefinance.providers.yfinance_provider.yf.Sector",
            side_effect=ConnectionError("fail"),
        ):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_sector_overview("technology")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_forward_estimates
# -----------------------------------------------------------------------


class TestGetForwardEstimates:
    def test_returns_estimates(self, provider: YFinanceProvider) -> None:
        rev_index = pd.Index(["0q", "+1q", "0y", "+1y"])
        rev_df = pd.DataFrame(
            {"avg": [90e9, 95e9, 380e9, 400e9], "growth": [0.05, 0.06, 0.04, 0.05]},
            index=rev_index,
        )
        eps_index = pd.Index(["0q", "+1q", "0y", "+1y"])
        eps_df = pd.DataFrame({"avg": [1.50, 1.60, 6.20, 6.80]}, index=eps_index)

        mock_ticker = MagicMock()
        mock_ticker.revenue_estimate = rev_df
        mock_ticker.earnings_estimate = eps_df
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_forward_estimates("AAPL")
        assert len(results) == 4
        assert results[0].symbol == "AAPL"
        assert results[0].revenue_estimate == 90e9
        assert results[0].source == "yfinance"

    def test_empty_estimates_returns_empty(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.revenue_estimate = pd.DataFrame()
        mock_ticker.earnings_estimate = pd.DataFrame()
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_forward_estimates("AAPL")
        assert results == []

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).revenue_estimate = PropertyMock(side_effect=ConnectionError("fail"))
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_forward_estimates("AAPL")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_financials
# -----------------------------------------------------------------------


def _make_financials_df(data: dict[pd.Timestamp, dict[str, float]]) -> pd.DataFrame:
    """Build a yfinance-style financial DataFrame: columns=dates, index=line-item labels."""
    return pd.DataFrame(data)


class TestGetFinancials:
    def _mock_ticker(self, df: pd.DataFrame, attr: str) -> MagicMock:
        mock_ticker = MagicMock()
        setattr(mock_ticker, attr, df)
        mock_ticker.info = {"currency": "USD"}
        return mock_ticker

    def test_income_statement_annual(self, provider: YFinanceProvider) -> None:
        col = pd.Timestamp("2024-09-28")
        df = _make_financials_df(
            {
                col: {
                    "Total Revenue": 416161e6,
                    "Cost Of Revenue": 220960e6,
                    "Gross Profit": 195201e6,
                    "Operating Income": 133050e6,
                    "Net Income": 112010e6,
                    "Basic EPS": 7.36,
                    "Diluted EPS": 7.34,
                    "EBITDA": 150000e6,
                }
            },
        )
        mock_ticker = self._mock_ticker(df, "income_stmt")
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_financials("AAPL", "income", "annual")
        assert len(results) == 1
        stmt = results[0]
        assert isinstance(stmt, IncomeStatement)
        assert stmt.symbol == "AAPL"
        assert stmt.period == "2024-FY"
        assert stmt.revenue == 416161e6
        assert stmt.eps_basic == 7.36
        assert stmt.currency == "USD"
        assert stmt.source == "yfinance"

    def test_balance_sheet_annual(self, provider: YFinanceProvider) -> None:
        col = pd.Timestamp("2024-09-28")
        df = _make_financials_df(
            {
                col: {
                    "Total Assets": 359500e6,
                    "Total Liabilities Net Minority Interest": 265000e6,
                    "Total Equity Gross Minority Interest": 94500e6,
                    "Cash And Cash Equivalents": 35934e6,
                    "Total Debt": 96000e6,
                }
            },
        )
        mock_ticker = self._mock_ticker(df, "balance_sheet")
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_financials("AAPL", "balance", "annual")
        assert len(results) == 1
        bs = results[0]
        assert isinstance(bs, BalanceSheet)
        assert bs.total_assets == 359500e6
        assert bs.period == "2024-FY"

    def test_cashflow_quarterly(self, provider: YFinanceProvider) -> None:
        col = pd.Timestamp("2024-06-29")
        df = _make_financials_df(
            {
                col: {
                    "Operating Cash Flow": 29587e6,
                    "Capital Expenditure": -2456e6,
                    "Free Cash Flow": 27131e6,
                    "Common Stock Dividend Paid": -3900e6,
                }
            },
        )
        mock_ticker = self._mock_ticker(df, "quarterly_cashflow")
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_financials("AAPL", "cashflow", "quarterly")
        assert len(results) == 1
        cf = results[0]
        assert isinstance(cf, CashFlow)
        assert cf.period == "2024-Q2"
        assert cf.free_cash_flow == 27131e6

    def test_unknown_statement_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_financials("AAPL", "unknown", "annual")
        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_empty_dataframe_returns_empty(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.income_stmt = pd.DataFrame()
        mock_ticker.info = {"currency": "USD"}
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_financials("AAPL", "income", "annual")
        assert results == []

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).income_stmt = PropertyMock(side_effect=ConnectionError("fail"))
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_financials("AAPL", "income", "annual")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_ratios
# -----------------------------------------------------------------------


class TestGetRatios:
    def _mock_info(self) -> dict[str, object]:
        return {
            "quoteType": "EQUITY",
            "trailingPE": 37.5,
            "priceToBook": 42.6,
            "priceToSalesTrailing12Months": 10.0,
            "debtToEquity": 79.5,
            "currentRatio": 1.07,
            "returnOnEquity": 1.41,
            "returnOnAssets": 0.26,
            "grossMargins": 0.479,
            "operatingMargins": 0.323,
            "profitMargins": 0.272,
            "dividendYield": 0.0034,
            "enterpriseValue": 4.6e12,
            "enterpriseToEbitda": 29.0,
            "enterpriseToRevenue": 10.3,
            "bookValue": 7.26,
            "revenuePerShare": 30.5,
            "quickRatio": 0.9,
        }

    def test_returns_single_ttm_record(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = self._mock_info()
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_ratios("AAPL", "annual")
        assert len(results) == 1
        r = results[0]
        from onefinance.core.models import FinancialRatios

        assert isinstance(r, FinancialRatios)
        assert r.symbol == "AAPL"
        assert r.period == "TTM"
        assert r.pe_ratio == pytest.approx(37.5)
        assert r.pb_ratio == pytest.approx(42.6)
        assert r.ps_ratio == pytest.approx(10.0)
        assert r.debt_to_equity == pytest.approx(79.5)
        assert r.gross_margin == pytest.approx(0.479)
        assert r.ev_to_ebitda == pytest.approx(29.0)
        assert r.quick_ratio == pytest.approx(0.9)
        assert r.source == "yfinance"

    def test_quarterly_period_still_returns_ttm(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = self._mock_info()
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_ratios("AAPL", "quarterly")
        assert len(results) == 1
        assert results[0].period == "TTM"

    def test_empty_info_raises_symbol_not_found(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_ratios("INVALID", "annual")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).info = PropertyMock(side_effect=ConnectionError("fail"))
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_ratios("AAPL", "annual")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_earnings
# -----------------------------------------------------------------------


class TestGetEarnings:
    def test_returns_earnings_records(self, provider: YFinanceProvider) -> None:
        index = pd.DatetimeIndex(
            [datetime(2025, 3, 29), datetime(2025, 6, 28)],
            name="quarter",
        )
        df = pd.DataFrame(
            {
                "epsActual": [1.65, 1.57],
                "epsEstimate": [1.56, 1.43],
                "epsDifference": [0.09, 0.14],
                "surprisePercent": [0.0577, 0.0979],
            },
            index=index,
        )
        mock_ticker = MagicMock()
        mock_ticker.earnings_history = df
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_earnings("AAPL")
        assert len(results) == 2
        from onefinance.core.models import EarningsRecord

        r = results[0]
        assert isinstance(r, EarningsRecord)
        assert r.symbol == "AAPL"
        assert r.period == "2025-Q1"
        assert r.fiscal_date == date(2025, 3, 29)
        assert r.eps_actual == pytest.approx(1.65)
        assert r.eps_estimate == pytest.approx(1.56)
        assert r.eps_surprise == pytest.approx(0.09)
        assert r.revenue_actual is None
        assert r.source == "yfinance"

    def test_period_label_q2(self, provider: YFinanceProvider) -> None:
        index = pd.DatetimeIndex([datetime(2025, 6, 28)], name="quarter")
        df = pd.DataFrame(
            {
                "epsActual": [1.57],
                "epsEstimate": [1.43],
                "epsDifference": [0.14],
                "surprisePercent": [0.098],
            },
            index=index,
        )
        mock_ticker = MagicMock()
        mock_ticker.earnings_history = df
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_earnings("AAPL")
        assert results[0].period == "2025-Q2"

    def test_empty_dataframe_returns_empty(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.earnings_history = pd.DataFrame()
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_earnings("AAPL")
        assert results == []

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).earnings_history = PropertyMock(side_effect=ConnectionError("fail"))
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_earnings("AAPL")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_insider_trades
# -----------------------------------------------------------------------


class TestGetInsiderTrades:
    def _mock_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "Shares": 50000,
                    "Value": 15551000.0,
                    "URL": "",
                    "Text": "Sale at price 311.02 per share.",
                    "Insider": "LEVINSON ARTHUR D",
                    "Position": "Director",
                    "Transaction": "",
                    "Start Date": "2026-05-27",
                    "Ownership": "D",
                },
                {
                    "Shares": 10000,
                    "Value": 2500000.0,
                    "URL": "",
                    "Text": "Purchase at price 250.00 per share.",
                    "Insider": "COOK TIMOTHY D",
                    "Position": "Chief Executive Officer",
                    "Transaction": "",
                    "Start Date": "2026-04-10",
                    "Ownership": "D",
                },
            ]
        )

    def test_returns_insider_trades(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.insider_transactions = self._mock_df()
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_insider_trades("AAPL")
        assert len(results) == 2
        from onefinance.core.models import InsiderTrade

        t = results[0]
        assert isinstance(t, InsiderTrade)
        assert t.symbol == "AAPL"
        assert t.insider_name == "LEVINSON ARTHUR D"
        assert t.insider_title == "Director"
        assert t.trade_type == "sell"
        assert t.shares == 50000.0
        assert t.total_value == pytest.approx(15551000.0)
        assert t.price_per_share == pytest.approx(311.02)
        assert t.filing_date == date(2026, 5, 27)
        assert t.source == "yfinance"

    def test_buy_trade_type(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.insider_transactions = self._mock_df()
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_insider_trades("AAPL")
        assert results[1].trade_type == "buy"

    def test_since_filter_excludes_older(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.insider_transactions = self._mock_df()
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_insider_trades("AAPL", since=date(2026, 5, 1))
        assert len(results) == 1
        assert results[0].filing_date == date(2026, 5, 27)

    def test_empty_dataframe_returns_empty(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.insider_transactions = pd.DataFrame()
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            results = provider.get_insider_trades("AAPL")
        assert results == []

    def test_network_error_raises(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        type(mock_ticker).insider_transactions = PropertyMock(side_effect=ConnectionError("fail"))
        with patch("onefinance.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_insider_trades("AAPL")
        assert exc_info.value.code == "NETWORK_ERROR"


# ---------------------------------------------------------------------------
# Index-symbol aliases (C5)
# ---------------------------------------------------------------------------


class TestIndexAliases:
    def test_bare_index_names_map_to_caret_form(self) -> None:
        from onefinance.providers.yfinance_provider import _yf_symbol

        assert _yf_symbol("VIX") == "^VIX"
        assert _yf_symbol("vix") == "^VIX"
        assert _yf_symbol("SOX") == "^SOX"
        assert _yf_symbol("SPX") == "^SPX"

    def test_regular_symbols_pass_through(self) -> None:
        from onefinance.providers.yfinance_provider import _yf_symbol

        assert _yf_symbol("AAPL") == "AAPL"
        assert _yf_symbol(" aapl ") == "AAPL"
        assert _yf_symbol("^VIX") == "^VIX"  # already caret-form
        assert _yf_symbol("000660.KS") == "000660.KS"
