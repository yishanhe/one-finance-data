"""Unit tests for MassiveProvider with mocked httpx responses."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from onefinance.core.errors import (
    ConfigError,
    NotSupportedError,
    ProviderError,
    RateLimitError,
)
from onefinance.core.models import (
    CompanyInfo,
    CorporateAction,
    NewsArticle,
    OptionChain,
    PriceBar,
    Quote,
)
from onefinance.providers.massive import MassiveProvider


@pytest.fixture
def provider() -> MassiveProvider:
    return MassiveProvider(api_key="test_key", timeout=5)


def _mock_response(data: Any, status_code: int = 200) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data) if not isinstance(data, str) else data
    resp.headers = {}
    return resp


# -----------------------------------------------------------------------
# Constructor
# -----------------------------------------------------------------------


class TestConstructor:
    def test_api_key_from_param(self) -> None:
        p = MassiveProvider(api_key="my_key")
        assert p._api_key == "my_key"

    def test_api_key_from_env(self) -> None:
        with patch.dict("os.environ", {"MASSIVE_API_KEY": "env_key"}):
            p = MassiveProvider()
            assert p._api_key == "env_key"

    def test_missing_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ConfigError):
                MassiveProvider()

    def test_name(self) -> None:
        assert MassiveProvider(api_key="k").name == "massive"


# -----------------------------------------------------------------------
# Rate-limit detection
# -----------------------------------------------------------------------


class TestRateLimitDetection:
    def test_http_429_raises(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 429)):
            with pytest.raises(RateLimitError):
                provider._get("/v2/snapshot/locale/us/markets/stocks/tickers/AAPL")

    def test_is_rate_limited_429(self, provider: MassiveProvider) -> None:
        resp = _mock_response({}, 429)
        assert provider.is_rate_limited(resp) is True

    def test_is_rate_limited_200_returns_false(self, provider: MassiveProvider) -> None:
        resp = _mock_response({"status": "OK"})
        assert provider.is_rate_limited(resp) is False

    def test_is_rate_limited_exception_with_429(self, provider: MassiveProvider) -> None:
        assert provider.is_rate_limited(Exception("HTTP 429 Too Many Requests")) is True

    def test_is_rate_limited_exception_no_match(self, provider: MassiveProvider) -> None:
        assert provider.is_rate_limited(Exception("connection timeout")) is False

    def test_is_rate_limited_non_httpx_returns_false(self, provider: MassiveProvider) -> None:
        assert provider.is_rate_limited("some string") is False

    def test_cooldown_returns_default(self, provider: MassiveProvider) -> None:
        resp = _mock_response({}, 429)
        assert provider.cooldown_for(resp) == provider._default_rate_limit_cooldown_s


# -----------------------------------------------------------------------
# _get — HTTP error handling
# -----------------------------------------------------------------------


class TestGetMethod:
    def test_403_raises_auth_error(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 403)):
            with pytest.raises(ProviderError) as exc_info:
                provider._get("/some/path")
        assert exc_info.value.code == "AUTH_ERROR"

    def test_404_raises_symbol_not_found(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 404)):
            with pytest.raises(ProviderError) as exc_info:
                provider._get("/some/path")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_500_raises_network_error(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 500)):
            with pytest.raises(ProviderError) as exc_info:
                provider._get("/some/path")
        assert exc_info.value.code == "NETWORK_ERROR"
        assert exc_info.value.retry_safe is True

    def test_non_retryable_error(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 400)):
            with pytest.raises(ProviderError) as exc_info:
                provider._get("/some/path")
        assert exc_info.value.retry_safe is False


# -----------------------------------------------------------------------
# get_price_history
# -----------------------------------------------------------------------

_PRICE_PAYLOAD = {
    "ticker": "AAPL",
    "resultsCount": 2,
    "adjusted": True,
    "results": [
        {
            "t": 1704153600000,  # 2024-01-02 UTC
            "o": 185.00,
            "h": 186.10,
            "l": 183.50,
            "c": 184.25,
            "v": 58414500.0,
            "vw": 184.80,
            "n": 612345,
        },
        {
            "t": 1704240000000,  # 2024-01-03 UTC
            "o": 184.00,
            "h": 185.50,
            "l": 182.00,
            "c": 185.64,
            "v": 52000000.0,
            "vw": 184.90,
            "n": 589123,
        },
    ],
    "status": "OK",
}


class TestGetPriceHistory:
    def test_returns_bars(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_PRICE_PAYLOAD)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 1, 31))
        assert len(bars) == 2
        assert all(isinstance(b, PriceBar) for b in bars)

    def test_fields_correct(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_PRICE_PAYLOAD)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 1, 31))
        b = bars[0]
        assert b.symbol == "AAPL"
        assert b.open == 185.00
        assert b.high == 186.10
        assert b.low == 183.50
        assert b.close == 184.25
        assert b.adj_close == 184.25
        assert b.volume == 58414500
        assert b.source == "massive"
        assert b.timestamp is None  # daily bars have no intraday timestamp

    def test_empty_results_returns_empty(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"status": "OK"})):
            bars = provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 1, 31))
        assert bars == []

    def test_unsupported_interval_raises(self, provider: MassiveProvider) -> None:
        with pytest.raises(ProviderError):
            provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 1, 31), interval="4h")

    def test_intraday_has_timestamp(self, provider: MassiveProvider) -> None:
        payload = {
            "results": [
                {"t": 1704153600000, "o": 185.0, "h": 186.0, "l": 184.0, "c": 185.5, "v": 1000.0}
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            bars = provider.get_price_history(
                "AAPL", date(2024, 1, 1), date(2024, 1, 31), interval="1h"
            )
        assert len(bars) == 1
        assert bars[0].timestamp is not None

    def test_malformed_bar_is_skipped(self, provider: MassiveProvider) -> None:
        payload = {
            "results": [
                {"t": 1704153600000, "o": 185.0, "h": 186.0, "l": 183.5, "c": 184.25, "v": 1000.0},
                {"t": "not_a_number"},  # malformed — missing OHLC, bad t
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 1, 31))
        assert len(bars) == 1


# -----------------------------------------------------------------------
# get_quote
# -----------------------------------------------------------------------

_QUOTE_PAYLOAD = {
    "status": "OK",
    "ticker": {
        "ticker": "AAPL",
        "todaysChangePerc": 1.23,
        "todaysChange": 1.84,
        "updated": 1704153600000,
        "day": {"o": 185.0, "h": 186.5, "l": 184.2, "c": 185.92, "v": 48273600.0},
        "lastTrade": {"p": 185.92, "s": 100, "t": 1704153600000000000},
        "lastQuote": {"P": 185.95, "p": 185.90, "S": 1, "s": 1, "t": 1704153600000000000},
        "prevDay": {"o": 184.0, "h": 185.5, "l": 183.0, "c": 184.38, "v": 52000000.0},
    },
}


class TestGetQuote:
    def test_returns_quote(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_QUOTE_PAYLOAD)):
            q = provider.get_quote("AAPL")
        assert isinstance(q, Quote)

    def test_fields_correct(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_QUOTE_PAYLOAD)):
            q = provider.get_quote("AAPL")
        assert q.symbol == "AAPL"
        assert q.price == 185.92
        assert q.bid == 185.90
        assert q.ask == 185.95
        assert q.volume == 48273600
        assert q.source == "massive"

    def test_no_ticker_raises(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"status": "OK"})):
            with pytest.raises(ProviderError):
                provider.get_quote("AAPL")

    def test_no_price_raises(self, provider: MassiveProvider) -> None:
        payload = {
            "status": "OK",
            "ticker": {
                "ticker": "AAPL",
                "day": {},
                "lastTrade": {},
                "lastQuote": {},
            },
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            with pytest.raises(ProviderError):
                provider.get_quote("AAPL")

    def test_fallback_to_day_close_when_no_last_trade(self, provider: MassiveProvider) -> None:
        payload = {
            "status": "OK",
            "ticker": {
                "ticker": "AAPL",
                "updated": 1704153600000,
                "day": {"c": 185.92, "v": 48273600.0},
                "lastTrade": {},
                "lastQuote": {},
            },
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            q = provider.get_quote("AAPL")
        assert q.price == 185.92

    def test_timestamp_from_updated_when_no_trade_ts(self, provider: MassiveProvider) -> None:
        payload = {
            "status": "OK",
            "ticker": {
                "ticker": "AAPL",
                "updated": 1704153600000,
                "day": {"c": 185.92, "v": 1000.0},
                "lastTrade": {},
                "lastQuote": {},
            },
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            q = provider.get_quote("AAPL")
        assert q.timestamp is not None

    def test_timestamp_fallback_to_now_when_no_updated(self, provider: MassiveProvider) -> None:
        payload = {
            "status": "OK",
            "ticker": {
                "ticker": "AAPL",
                "day": {"c": 185.92, "v": 1000.0},
                "lastTrade": {},
                "lastQuote": {},
            },
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            q = provider.get_quote("AAPL")
        assert q.timestamp is not None

    def test_bad_trade_timestamp_falls_back_to_now(self, provider: MassiveProvider) -> None:
        # Overflow in fromtimestamp triggers the except branch
        payload = {
            "status": "OK",
            "ticker": {
                "ticker": "AAPL",
                "day": {"c": 185.92, "v": 1000.0},
                "lastTrade": {"p": 185.92, "t": 9e30},  # absurdly large → OverflowError
                "lastQuote": {},
            },
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            q = provider.get_quote("AAPL")
        assert q.price == 185.92

    def test_bad_updated_timestamp_falls_back_to_now(self, provider: MassiveProvider) -> None:
        payload = {
            "status": "OK",
            "ticker": {
                "ticker": "AAPL",
                "updated": 9e30,  # no lastTrade.t; bad updated → OverflowError
                "day": {"c": 185.92, "v": 1000.0},
                "lastTrade": {},
                "lastQuote": {},
            },
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            q = provider.get_quote("AAPL")
        assert q.price == 185.92


# -----------------------------------------------------------------------
# get_info
# -----------------------------------------------------------------------

_INFO_PAYLOAD = {
    "status": "OK",
    "results": {
        "ticker": "AAPL",
        "name": "Apple Inc.",
        "market": "stocks",
        "locale": "us",
        "primary_exchange": "XNAS",
        "type": "CS",
        "currency_name": "usd",
        "description": "Apple Inc. designs, manufactures and markets smartphones.",
        "homepage_url": "https://www.apple.com",
        "total_employees": 154000,
        "market_cap": 2950000000000.0,
        "sic_description": "Electronic Computers",
        "share_class_shares_outstanding": 15441000000,
    },
}


class TestGetInfo:
    def test_returns_company_info(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_INFO_PAYLOAD)):
            info = provider.get_info("AAPL")
        assert isinstance(info, CompanyInfo)

    def test_fields_correct(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_INFO_PAYLOAD)):
            info = provider.get_info("AAPL")
        assert info.symbol == "AAPL"
        assert info.name == "Apple Inc."
        assert info.exchange == "XNAS"
        assert info.industry == "Electronic Computers"
        assert info.sector is None  # Massive doesn't classify by sector
        assert info.country == "US"
        assert info.currency == "USD"
        assert info.market_cap == 2950000000000.0
        assert info.employees == 154000
        assert info.website == "https://www.apple.com"
        assert info.source == "massive"

    def test_no_results_raises(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"status": "OK"})):
            with pytest.raises(ProviderError):
                provider.get_info("AAPL")

    def test_non_dict_results_raises(self, provider: MassiveProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({"status": "OK", "results": []}),
        ):
            with pytest.raises(ProviderError):
                provider.get_info("AAPL")

    def test_short_currency_name_sets_none(self, provider: MassiveProvider) -> None:
        payload = dict(_INFO_PAYLOAD)
        payload["results"] = dict(_INFO_PAYLOAD["results"])  # type: ignore[arg-type]
        payload["results"]["currency_name"] = "US"  # type: ignore[index]
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            info = provider.get_info("AAPL")
        assert info.currency is None

    def test_empty_locale_sets_none_country(self, provider: MassiveProvider) -> None:
        payload = dict(_INFO_PAYLOAD)
        payload["results"] = dict(_INFO_PAYLOAD["results"])  # type: ignore[arg-type]
        payload["results"]["locale"] = ""  # type: ignore[index]
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            info = provider.get_info("AAPL")
        assert info.country is None


# -----------------------------------------------------------------------
# get_news
# -----------------------------------------------------------------------

_NEWS_PAYLOAD = {
    "status": "OK",
    "results": [
        {
            "id": "abc123",
            "publisher": {"name": "Reuters", "homepage_url": "https://reuters.com"},
            "title": "Apple Reports Record Revenue",
            "author": "Jane Smith",
            "published_utc": "2024-01-05T14:30:00Z",
            "article_url": "https://reuters.com/apple-revenue",
            "tickers": ["AAPL"],
            "description": "Apple Inc. reported record revenue for Q1 2024.",
        },
        {
            "id": "def456",
            "publisher": {"name": "Bloomberg"},
            "title": "Tech Sector Update",
            "author": "",
            "published_utc": "2024-01-05T12:00:00Z",
            "article_url": "https://bloomberg.com/tech-update",
            "tickers": ["AAPL", "MSFT"],
            "description": "Overview of the tech sector.",
        },
    ],
}


class TestGetNews:
    def test_returns_articles(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_NEWS_PAYLOAD)):
            articles = provider.get_news("AAPL")
        assert len(articles) == 2
        assert all(isinstance(a, NewsArticle) for a in articles)

    def test_fields_correct(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_NEWS_PAYLOAD)):
            articles = provider.get_news("AAPL")
        a = articles[0]
        assert a.title == "Apple Reports Record Revenue"
        assert a.publisher == "Reuters"
        assert a.link == "https://reuters.com/apple-revenue"
        assert a.summary == "Apple Inc. reported record revenue for Q1 2024."
        assert a.source == "massive"

    def test_empty_results_returns_empty(self, provider: MassiveProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response({"status": "OK", "results": []})
        ):
            articles = provider.get_news("AAPL")
        assert articles == []

    def test_skips_article_with_no_title(self, provider: MassiveProvider) -> None:
        payload = {
            "results": [
                {
                    "publisher": {"name": "X"},
                    "title": "",
                    "article_url": "https://example.com/a",
                    "published_utc": "2024-01-05T14:30:00Z",
                },
                {
                    "publisher": {"name": "Y"},
                    "title": "Valid Title",
                    "article_url": "https://example.com/b",
                    "published_utc": "2024-01-05T14:30:00Z",
                },
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            articles = provider.get_news("AAPL")
        assert len(articles) == 1

    def test_skips_article_with_no_url(self, provider: MassiveProvider) -> None:
        payload = {
            "results": [
                {
                    "publisher": {"name": "X"},
                    "title": "No URL",
                    "article_url": "",
                    "published_utc": "2024-01-05T14:30:00Z",
                },
                {
                    "publisher": {"name": "Y"},
                    "title": "Valid",
                    "article_url": "https://example.com/valid",
                    "published_utc": "2024-01-05T14:30:00Z",
                },
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            articles = provider.get_news("AAPL")
        assert len(articles) == 1

    def test_bad_timestamp_falls_back_to_now(self, provider: MassiveProvider) -> None:
        payload = {
            "results": [
                {
                    "publisher": {"name": "X"},
                    "title": "Some News",
                    "article_url": "https://example.com/news",
                    "published_utc": "NOT_A_DATE",
                },
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            articles = provider.get_news("AAPL")
        assert len(articles) == 1

    def test_missing_publisher_uses_unknown(self, provider: MassiveProvider) -> None:
        payload = {
            "results": [
                {
                    "publisher": {},
                    "title": "Some News",
                    "article_url": "https://example.com/news",
                    "published_utc": "2024-01-05T14:30:00Z",
                },
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            articles = provider.get_news("AAPL")
        assert articles[0].publisher == "Unknown"

    def test_malformed_item_is_skipped(self, provider: MassiveProvider) -> None:
        # publisher as string (not dict) causes AttributeError → outer except → skip
        payload = {
            "results": [
                {
                    "publisher": "Reuters",  # string, not dict — .get() fails
                    "title": "Broken Item",
                    "article_url": "https://example.com/broken",
                    "published_utc": "2024-01-05T14:30:00Z",
                },
                {
                    "publisher": {"name": "Bloomberg"},
                    "title": "Good Item",
                    "article_url": "https://bloomberg.com/good",
                    "published_utc": "2024-01-05T12:00:00Z",
                },
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            articles = provider.get_news("AAPL")
        assert len(articles) == 1
        assert articles[0].title == "Good Item"


# -----------------------------------------------------------------------
# get_corporate_actions
# -----------------------------------------------------------------------

_DIV_PAYLOAD = {
    "status": "OK",
    "results": [
        {
            "ticker": "AAPL",
            "ex_dividend_date": "2024-02-09",
            "cash_amount": 0.24,
            "declaration_date": "2024-02-01",
            "frequency": 4,
            "pay_date": "2024-02-15",
        }
    ],
}

_SPLIT_PAYLOAD = {
    "status": "OK",
    "results": [
        {
            "execution_date": "2020-08-31",
            "split_from": 1,
            "split_to": 4,
            "ticker": "AAPL",
        }
    ],
}


class TestGetCorporateActions:
    def test_returns_dividends_and_splits(self, provider: MassiveProvider) -> None:
        responses = [_mock_response(_DIV_PAYLOAD), _mock_response(_SPLIT_PAYLOAD)]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        assert len(actions) == 2
        assert all(isinstance(a, CorporateAction) for a in actions)

    def test_dividend_fields(self, provider: MassiveProvider) -> None:
        responses = [_mock_response(_DIV_PAYLOAD), _mock_response({"results": []})]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        div = actions[0]
        assert div.action_type == "dividend"
        assert div.date == date(2024, 2, 9)
        assert div.amount == 0.24
        assert div.source == "massive"

    def test_split_ratio_correct(self, provider: MassiveProvider) -> None:
        responses = [_mock_response({"results": []}), _mock_response(_SPLIT_PAYLOAD)]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        split = actions[0]
        assert split.action_type == "split"
        assert split.split_ratio == 4.0  # split_to / split_from = 4/1

    def test_sorted_descending(self, provider: MassiveProvider) -> None:
        div_payload = {
            "results": [
                {"ex_dividend_date": "2024-02-09", "cash_amount": 0.24, "ticker": "AAPL"},
                {"ex_dividend_date": "2023-11-10", "cash_amount": 0.24, "ticker": "AAPL"},
            ]
        }
        responses = [_mock_response(div_payload), _mock_response({"results": []})]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        assert actions[0].date > actions[1].date

    def test_skips_dividend_with_no_date(self, provider: MassiveProvider) -> None:
        payload = {
            "results": [
                {"ex_dividend_date": "", "cash_amount": 0.24},
                {"ex_dividend_date": "2024-02-09", "cash_amount": 0.24},
            ]
        }
        responses = [_mock_response(payload), _mock_response({"results": []})]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        assert len(actions) == 1

    def test_skips_split_with_no_date(self, provider: MassiveProvider) -> None:
        payload = {
            "results": [
                {"execution_date": "", "split_from": 1, "split_to": 4},
                {"execution_date": "2020-08-31", "split_from": 1, "split_to": 4},
            ]
        }
        responses = [_mock_response({"results": []}), _mock_response(payload)]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        assert len(actions) == 1

    def test_empty_results_returns_empty(self, provider: MassiveProvider) -> None:
        responses = [_mock_response({"results": []}), _mock_response({"results": []})]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        assert actions == []

    def test_malformed_dividend_is_skipped(self, provider: MassiveProvider) -> None:
        # Invalid date string causes parse_iso_date to raise → except → continue
        payload = {
            "results": [
                {"ex_dividend_date": "not-a-date", "cash_amount": 0.24},
                {"ex_dividend_date": "2024-02-09", "cash_amount": 0.24},
            ]
        }
        responses = [_mock_response(payload), _mock_response({"results": []})]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        assert len(actions) == 1

    def test_malformed_split_is_skipped(self, provider: MassiveProvider) -> None:
        payload = {
            "results": [
                {"execution_date": "not-a-date", "split_from": 1, "split_to": 4},
                {"execution_date": "2020-08-31", "split_from": 1, "split_to": 4},
            ]
        }
        responses = [_mock_response({"results": []}), _mock_response(payload)]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        assert len(actions) == 1


# -----------------------------------------------------------------------
# supports() capability discovery
# -----------------------------------------------------------------------


class TestSupports:
    def test_supported_endpoints(self) -> None:
        p = MassiveProvider(api_key="k")
        assert p.supports("price_history")
        assert p.supports("quote")
        assert p.supports("info")
        assert p.supports("news")
        assert p.supports("corporate_actions")
        assert p.supports("options_expirations")
        assert p.supports("option_chain")

    def test_unsupported_endpoints(self) -> None:
        p = MassiveProvider(api_key="k")
        assert not p.supports("financials")
        assert not p.supports("ratios")
        assert not p.supports("earnings")
        assert not p.supports("insider_trades")
        assert not p.supports("dcf")


# -----------------------------------------------------------------------
# Options — expirations + chain (plan-gated)
# -----------------------------------------------------------------------


_CONTRACTS_PAYLOAD = {
    "results": [
        {"expiration_date": "2026-06-19", "strike_price": 150, "contract_type": "call"},
        {"expiration_date": "2026-01-16", "strike_price": 150, "contract_type": "put"},
        {"expiration_date": "2026-06-19", "strike_price": 160, "contract_type": "put"},
        {"strike_price": 170, "contract_type": "call"},  # missing expiration → skipped
    ]
}

_SNAPSHOT_PAYLOAD = {
    "results": [
        {
            "details": {
                "contract_type": "call",
                "strike_price": 150.0,
                "ticker": "O:AAPL260619C00150000",
                "expiration_date": "2026-06-19",
            },
            "last_quote": {"bid": 12.3, "ask": 12.5},
            "last_trade": {"price": 12.4},
            "day": {"volume": 1500},
            "open_interest": 4200,
            "implied_volatility": 0.28,
        },
        {
            "details": {
                "contract_type": "put",
                "strike_price": 140.0,
                "ticker": "O:AAPL260619P00140000",
                "expiration_date": "2026-06-19",
            },
            "last_quote": {"bid": 3.1, "ask": 3.3},
            "last_trade": {"price": 3.2},
            "day": {"volume": 900},
            "open_interest": 2100,
            "implied_volatility": 0.31,
        },
        {"details": {"contract_type": "call"}},  # no strike/ticker → skipped
    ]
}


class TestGetOptionsExpirations:
    def test_returns_sorted_unique_dates(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_CONTRACTS_PAYLOAD)):
            dates = provider.get_options_expirations("AAPL")
        assert dates == [date(2026, 1, 16), date(2026, 6, 19)]

    def test_empty_results(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"results": []})):
            assert provider.get_options_expirations("AAPL") == []

    def test_403_raises_not_supported(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 403)):
            with pytest.raises(NotSupportedError):
                provider.get_options_expirations("AAPL")

    def test_follows_pagination(self, provider: MassiveProvider) -> None:
        page1 = {
            "results": [{"expiration_date": "2026-01-16"}],
            "next_url": "https://api.massive.com/v3/reference/options/contracts?cursor=abc",
        }
        page2 = {"results": [{"expiration_date": "2026-06-19"}]}
        with patch.object(
            provider._client, "get", side_effect=[_mock_response(page1), _mock_response(page2)]
        ) as mock_get:
            dates = provider.get_options_expirations("AAPL")
        # Both pages merged → both dates present.
        assert dates == [date(2026, 1, 16), date(2026, 6, 19)]
        assert mock_get.call_count == 2


class TestGetOptionChain:
    def test_returns_chain(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_SNAPSHOT_PAYLOAD)):
            chain = provider.get_option_chain("AAPL", date(2026, 6, 19))
        assert isinstance(chain, OptionChain)
        assert chain.symbol == "AAPL"
        assert chain.expiration_date == date(2026, 6, 19)
        assert len(chain.calls) == 1
        assert len(chain.puts) == 1

    def test_contract_fields(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_SNAPSHOT_PAYLOAD)):
            chain = provider.get_option_chain("AAPL", date(2026, 6, 19))
        call = chain.calls[0]
        assert call.contract_symbol == "O:AAPL260619C00150000"
        assert call.strike == 150.0
        assert call.bid == 12.3
        assert call.ask == 12.5
        assert call.last_price == 12.4
        assert call.volume == 1500
        assert call.open_interest == 4200
        assert call.implied_volatility == 0.28

    def test_403_raises_not_supported(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 403)):
            with pytest.raises(NotSupportedError):
                provider.get_option_chain("AAPL", date(2026, 6, 19))

    def test_malformed_contract_skipped(self, provider: MassiveProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_SNAPSHOT_PAYLOAD)):
            chain = provider.get_option_chain("AAPL", date(2026, 6, 19))
        # 3 results, 1 has no strike/ticker → 2 valid contracts.
        assert len(chain.calls) + len(chain.puts) == 2

    def test_follows_pagination(self, provider: MassiveProvider) -> None:
        page1 = {
            "results": [
                {"details": {"contract_type": "call", "strike_price": 150.0, "ticker": "O:C1"}}
            ],
            "next_url": "https://api.massive.com/v3/snapshot/options/AAPL?cursor=xyz",
        }
        page2 = {
            "results": [
                {"details": {"contract_type": "put", "strike_price": 140.0, "ticker": "O:P1"}}
            ]
        }
        with patch.object(
            provider._client, "get", side_effect=[_mock_response(page1), _mock_response(page2)]
        ) as mock_get:
            chain = provider.get_option_chain("AAPL", date(2026, 6, 19))
        assert len(chain.calls) == 1
        assert len(chain.puts) == 1
        assert mock_get.call_count == 2
