"""Unit tests for TwelveDataProvider with mocked httpx responses."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from onefinance.core.errors import ConfigError, ProviderError, RateLimitError
from onefinance.core.models import CompanyInfo, EarningsRecord, NewsArticle, PriceBar, Quote
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

    def test_invalid_timestamp_falls_back(self, provider: TwelveDataProvider) -> None:
        data = dict(
            symbol="AAPL",
            close="185.64",
            volume="52000000",
            timestamp="not-a-number",
        )
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            q = provider.get_quote("AAPL")
        assert q.price == 185.64


# -----------------------------------------------------------------------
# get_quotes
# -----------------------------------------------------------------------


class TestGetQuotes:
    _batch_data = {
        "AAPL": {
            "symbol": "AAPL",
            "close": "185.64",
            "volume": "52000000",
            "timestamp": 1704204000,
        },
        "MSFT": {
            "symbol": "MSFT",
            "close": "400.00",
            "volume": "30000000",
            "timestamp": 1704204000,
        },
    }

    def test_returns_multiple_quotes(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._batch_data)):
            quotes = provider.get_quotes(["AAPL", "MSFT"])
        assert len(quotes) == 2
        assert quotes[0].symbol == "AAPL"
        assert quotes[0].price == 185.64
        assert quotes[1].symbol == "MSFT"
        assert quotes[1].price == 400.00

    def test_single_symbol_dict_normalization(self, provider: TwelveDataProvider) -> None:
        # TwelveData returns a flat dict if only 1 symbol is requested in a batch
        single_data = {
            "symbol": "AAPL",
            "close": "185.64",
            "volume": "52000000",
            "timestamp": 1704204000,
        }
        with patch.object(provider._client, "get", return_value=_mock_response(single_data)):
            quotes = provider.get_quotes(["AAPL"])
        assert len(quotes) == 1
        assert quotes[0].symbol == "AAPL"
        assert quotes[0].price == 185.64

    def test_missing_symbol_raises(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._batch_data)):
            with pytest.raises(ProviderError) as exc_info:
                # FAKE is missing from response
                provider.get_quotes(["AAPL", "FAKE"])
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_invalid_batch_response(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quotes(["AAPL"])
        assert exc_info.value.code == "API_ERROR"


# -----------------------------------------------------------------------
# _rate_limit_signals
# -----------------------------------------------------------------------


class TestRateLimitSignals:
    def test_non_200_non_429_returns_false(self, provider: TwelveDataProvider) -> None:
        resp = _mock_response("Not Found", status_code=404)
        is_limit, _ = provider._rate_limit_signals(resp)
        assert is_limit is False

    def test_json_parse_error_returns_false(self, provider: TwelveDataProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.side_effect = ValueError("bad json")
        is_limit, _ = provider._rate_limit_signals(resp)
        assert is_limit is False

    def test_json_code_429_returns_true(self, provider: TwelveDataProvider) -> None:
        resp = _mock_response({"code": 429, "message": "rate limit"})
        is_limit, _ = provider._rate_limit_signals(resp)
        assert is_limit is True


# -----------------------------------------------------------------------
# _get — non-200 HTTP
# -----------------------------------------------------------------------


class TestGetHelper:
    def test_non_200_raises_provider_error(self, provider: TwelveDataProvider) -> None:
        resp = _mock_response("Not Found", status_code=404)
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError) as exc_info:
                provider._get("time_series")
        assert exc_info.value.code == "NETWORK_ERROR"

    def test_5xx_is_retryable(self, provider: TwelveDataProvider) -> None:
        resp = _mock_response("Server Error", status_code=500)
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError) as exc_info:
                provider._get("time_series")
        assert exc_info.value.retry_safe is True

    def test_4xx_not_retryable(self, provider: TwelveDataProvider) -> None:
        resp = _mock_response("Bad Request", status_code=400)
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError) as exc_info:
                provider._get("time_series")
        assert exc_info.value.retry_safe is False


# -----------------------------------------------------------------------
# intraday bars
# -----------------------------------------------------------------------


class TestIntradayBars:
    _intraday_data = {
        "values": [
            {
                "datetime": "2024-01-02 09:30:00",
                "open": "184.22",
                "high": "185.00",
                "low": "184.00",
                "close": "184.75",
                "volume": "1500000",
            }
        ]
    }

    def test_intraday_timestamp_parsed(self, provider: TwelveDataProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._intraday_data)
        ):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 2), "5m")
        assert len(bars) == 1
        assert bars[0].timestamp is not None

    def test_bad_bar_skipped(self, provider: TwelveDataProvider) -> None:
        data = {
            "values": [
                {
                    "datetime": "2024-01-02",
                    "open": "bad",
                    "high": "185.00",
                    "low": "184.00",
                    "close": "184.75",
                    "volume": "1500000",
                }
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 2))
        assert bars == []


# -----------------------------------------------------------------------
# is_rate_limited / cooldown_for
# -----------------------------------------------------------------------


class TestIsRateLimited:
    def test_429_http_response(self, provider: TwelveDataProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 429
        assert provider.is_rate_limited(resp) is True

    def test_json_code_429_response(self, provider: TwelveDataProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"code": 429}
        assert provider.is_rate_limited(resp) is True

    def test_json_parse_error_returns_false(self, provider: TwelveDataProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.side_effect = ValueError("bad json")
        assert provider.is_rate_limited(resp) is False

    def test_exception_with_429(self, provider: TwelveDataProvider) -> None:
        exc = Exception("HTTP 429")
        assert provider.is_rate_limited(exc) is True

    def test_non_response_returns_false(self, provider: TwelveDataProvider) -> None:
        assert provider.is_rate_limited("some_data") is False

    def test_cooldown_returns_60(self, provider: TwelveDataProvider) -> None:
        assert provider.cooldown_for(None) == 60.0


# -----------------------------------------------------------------------
# get_info
# -----------------------------------------------------------------------

_PROFILE_PAYLOAD = {
    "symbol": "AAPL",
    "name": "Apple Inc",
    "exchange": "NASDAQ",
    "sector": "Technology",
    "industry": "Consumer Electronics",
    "description": "Apple Inc. designs...",
    "website": "https://www.apple.com",
    "employees": 164000,
    "country": "US",
    "currency": "USD",
}


class TestGetInfo:
    def test_returns_company_info(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_PROFILE_PAYLOAD)):
            info = provider.get_info("AAPL")
        assert isinstance(info, CompanyInfo)
        assert info.symbol == "AAPL"
        assert info.name == "Apple Inc"
        assert info.sector == "Technology"
        assert info.employees == 164000
        assert info.source == "twelve_data"

    def test_missing_name_raises(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({})):
            with pytest.raises(ProviderError, match="No profile found"):
                provider.get_info("AAPL")

    def test_error_response_raises(self, provider: TwelveDataProvider) -> None:
        payload = {"status": "error", "message": "Not found"}
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            with pytest.raises(ProviderError):
                provider.get_info("INVALID")


# -----------------------------------------------------------------------
# get_news
# -----------------------------------------------------------------------

_NEWS_LIST_PAYLOAD = [
    {
        "title": "Apple hits record high",
        "url": "https://example.com/1",
        "source": {"name": "Reuters"},
        "published_at": "2024-01-15T10:30:00Z",
        "description": "Apple shares hit a new record.",
    },
    {
        "title": "iPhone sales surge",
        "url": "https://example.com/2",
        "source": {"name": "Bloomberg"},
        "published_at": "2024-01-14T09:00:00Z",
        "description": None,
    },
]

_NEWS_DICT_PAYLOAD = {"status": "ok", "data": _NEWS_LIST_PAYLOAD}


class TestGetNews:
    def test_returns_list_format(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_NEWS_LIST_PAYLOAD)):
            news = provider.get_news("AAPL")
        assert len(news) == 2
        assert isinstance(news[0], NewsArticle)
        assert news[0].title == "Apple hits record high"
        assert news[0].publisher == "Reuters"
        assert news[0].source == "twelve_data"

    def test_returns_dict_format(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_NEWS_DICT_PAYLOAD)):
            news = provider.get_news("AAPL")
        assert len(news) == 2

    def test_empty_returns_empty_list(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            news = provider.get_news("AAPL")
        assert news == []

    def test_limit_respected(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_NEWS_LIST_PAYLOAD)):
            news = provider.get_news("AAPL", limit=1)
        assert len(news) == 1


# -----------------------------------------------------------------------
# get_earnings
# -----------------------------------------------------------------------

_EARNINGS_PAYLOAD = {
    "symbol": "AAPL",
    "status": "ok",
    "earnings": [
        {
            "date": "2024-01-25",
            "period": "Q1 2024",
            "actual_eps": 2.18,
            "consensus_eps": 2.10,
            "surprise_eps": 0.08,
            "revenue_actual": 119575000000,
            "revenue_estimate": 117930000000,
        },
        {
            "date": "2023-10-26",
            "period": "Q4 2023",
            "actual_eps": 1.46,
            "consensus_eps": 1.39,
            "surprise_eps": 0.07,
            "revenue_actual": None,
            "revenue_estimate": None,
        },
    ],
}


class TestGetEarnings:
    def test_returns_earnings_list(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_EARNINGS_PAYLOAD)):
            records = provider.get_earnings("AAPL")
        assert len(records) == 2
        assert isinstance(records[0], EarningsRecord)
        assert records[0].eps_actual == pytest.approx(2.18)
        assert records[0].eps_estimate == pytest.approx(2.10)
        assert records[0].revenue_actual == pytest.approx(119575000000)
        assert records[0].source == "twelve_data"

    def test_none_revenue_fields(self, provider: TwelveDataProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_EARNINGS_PAYLOAD)):
            records = provider.get_earnings("AAPL")
        assert records[1].revenue_actual is None
        assert records[1].revenue_estimate is None

    def test_empty_earnings(self, provider: TwelveDataProvider) -> None:
        payload = {"symbol": "AAPL", "status": "ok", "earnings": []}
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            records = provider.get_earnings("AAPL")
        assert records == []

    def test_quarterly_passes_period(self, provider: TwelveDataProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(_EARNINGS_PAYLOAD)
        ) as mock_get:
            provider.get_earnings("AAPL", period="quarterly")
        call_kwargs = mock_get.call_args
        assert "quarterly" in str(call_kwargs)


# -----------------------------------------------------------------------
# Supports
# -----------------------------------------------------------------------


class TestTwelveDataSupports:
    def test_new_endpoints_supported(self, provider: TwelveDataProvider) -> None:
        assert provider.supports("info")
        assert provider.supports("news")
        assert provider.supports("earnings")

    def test_still_unsupported(self, provider: TwelveDataProvider) -> None:
        assert not provider.supports("dcf")
        assert not provider.supports("insider_trades")
        assert not provider.supports("option_chain")
