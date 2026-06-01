"""Unit tests for FinnhubProvider with mocked httpx responses."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from onefinance.core.errors import ConfigError, ProviderError, RateLimitError
from onefinance.core.models import (
    AnalystData,
    CompanyInfo,
    EarningsCalendarEntry,
    EarningsRecord,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    NewsArticle,
    PriceBar,
    Quote,
)
from onefinance.providers.finnhub import FinnhubProvider, _xbrl_float, _xbrl_float_opt


@pytest.fixture
def provider() -> FinnhubProvider:
    return FinnhubProvider(api_key="test_key", timeout=5)


def _mock_response(
    data: Any,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data) if not isinstance(data, str) else data
    resp.headers = headers or {}
    return resp


# -----------------------------------------------------------------------
# Constructor
# -----------------------------------------------------------------------


class TestConstructor:
    def test_api_key_from_param(self) -> None:
        p = FinnhubProvider(api_key="my_key")
        assert p._api_key == "my_key"

    def test_api_key_from_env(self) -> None:
        with patch.dict("os.environ", {"FINNHUB_API_KEY": "env_key"}):
            p = FinnhubProvider()
            assert p._api_key == "env_key"

    def test_missing_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ConfigError):
                FinnhubProvider()

    def test_name(self) -> None:
        p = FinnhubProvider(api_key="k")
        assert p.name == "finnhub"


# -----------------------------------------------------------------------
# Rate-limit detection
# -----------------------------------------------------------------------


class TestRateLimitDetection:
    def test_http_429_raises_rate_limit(self, provider: FinnhubProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({}, 429, {"Retry-After": "120"}),
        ):
            with pytest.raises(RateLimitError) as exc_info:
                provider._get("quote", {"symbol": "AAPL"})
        assert exc_info.value.retry_after_seconds == 120

    def test_http_429_default_cooldown(self, provider: FinnhubProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({}, 429, {}),
        ):
            with pytest.raises(RateLimitError) as exc_info:
                provider._get("quote", {})
        assert exc_info.value.retry_after_seconds == 60

    def test_non_200_raises_provider_error(self, provider: FinnhubProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response("Server Error", 500),
        ):
            with pytest.raises(ProviderError):
                provider._get("quote", {})


# -----------------------------------------------------------------------
# get_price_history
# -----------------------------------------------------------------------


class TestGetPriceHistory:
    _candle_data = {
        "c": [185.64, 186.01],
        "h": [186.50, 186.90],
        "l": [184.00, 185.20],
        "o": [184.50, 185.64],
        "t": [1704153600, 1704240000],  # 2024-01-02, 2024-01-03 UTC
        "v": [52000000, 48000000],
        "s": "ok",
    }

    def test_returns_price_bars(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._candle_data)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))
        assert len(bars) == 2
        assert all(isinstance(b, PriceBar) for b in bars)
        assert bars[0].symbol == "AAPL"
        assert bars[0].close == 185.64
        assert bars[0].volume == 52000000
        assert bars[0].source == "finnhub"

    def test_no_data_returns_empty(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"s": "no_data"})):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))
        assert bars == []

    def test_symbol_uppercased(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._candle_data)):
            bars = provider.get_price_history("aapl", date(2024, 1, 2), date(2024, 1, 3))
        assert bars[0].symbol == "AAPL"

    def test_adj_close_equals_close(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._candle_data)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))
        assert bars[0].adj_close == bars[0].close


# -----------------------------------------------------------------------
# get_quote
# -----------------------------------------------------------------------


class TestGetQuote:
    _quote_data = {
        "c": 185.64,
        "d": 1.64,
        "dp": 0.89,
        "h": 186.00,
        "l": 184.50,
        "o": 184.80,
        "pc": 184.00,
        "t": 1704204000,
        "v": 52000000,
    }

    def test_returns_quote(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._quote_data)):
            q = provider.get_quote("AAPL")
        assert isinstance(q, Quote)
        assert q.symbol == "AAPL"
        assert q.price == 185.64
        assert q.source == "finnhub"

    def test_no_data_raises(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"c": None})):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("AAPL")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_symbol_uppercased(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._quote_data)):
            q = provider.get_quote("aapl")
        assert q.symbol == "AAPL"


# -----------------------------------------------------------------------
# get_info
# -----------------------------------------------------------------------


class TestGetInfo:
    _profile_data = {
        "name": "Apple Inc",
        "exchange": "NASDAQ NMS - GLOBAL MARKET",
        "finnhubIndustry": "Technology",
        "country": "US",
        "currency": "USD",
        "marketCapitalization": 2900000.0,
        "weburl": "https://www.apple.com/",
        "ticker": "AAPL",
    }

    def test_returns_company_info(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._profile_data)):
            info = provider.get_info("AAPL")
        assert isinstance(info, CompanyInfo)
        assert info.name == "Apple Inc"
        assert info.industry == "Technology"
        assert info.country == "US"
        assert info.currency == "USD"
        assert info.source == "finnhub"

    def test_market_cap_converted_from_millions(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._profile_data)):
            info = provider.get_info("AAPL")
        assert info.market_cap == 2_900_000.0 * 1_000_000

    def test_empty_response_raises(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({})):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_info("FAKE")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"


# -----------------------------------------------------------------------
# get_financials
# -----------------------------------------------------------------------


class TestGetFinancials:
    _income_data = {
        "data": [
            {
                "endDate": "2023-09-30",
                "year": 2023,
                "quarter": 0,
                "report": {
                    "ic": [
                        {
                            "concept": "us-gaap:Revenues",
                            "label": "Revenue",
                            "unit": "USD",
                            "value": 383285000000,
                        },
                        {
                            "concept": "us-gaap:CostOfRevenue",
                            "label": "Cost",
                            "unit": "USD",
                            "value": 214137000000,
                        },
                        {
                            "concept": "us-gaap:GrossProfit",
                            "label": "Gross Profit",
                            "unit": "USD",
                            "value": 169148000000,
                        },
                        {
                            "concept": "us-gaap:OperatingIncomeLoss",
                            "label": "Op Income",
                            "unit": "USD",
                            "value": 114301000000,
                        },
                        {
                            "concept": "us-gaap:NetIncomeLoss",
                            "label": "Net Income",
                            "unit": "USD",
                            "value": 96995000000,
                        },
                        {
                            "concept": "us-gaap:EarningsPerShareBasic",
                            "label": "EPS Basic",
                            "unit": "USD/shares",
                            "value": 6.16,
                        },
                        {
                            "concept": "us-gaap:EarningsPerShareDiluted",
                            "label": "EPS Diluted",
                            "unit": "USD/shares",
                            "value": 6.13,
                        },
                    ],
                    "bs": [],
                    "cf": [],
                },
            }
        ],
    }

    def test_income_statement(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._income_data)):
            results = provider.get_financials("AAPL", "income", "annual")
        assert len(results) == 1
        stmt = results[0]
        assert isinstance(stmt, IncomeStatement)
        assert stmt.revenue == 383285000000
        assert stmt.net_income == 96995000000
        assert stmt.period == "2023-FY"
        assert stmt.source == "finnhub"

    def test_invalid_statement_raises(self, provider: FinnhubProvider) -> None:
        with pytest.raises(ProviderError) as exc_info:
            provider.get_financials("AAPL", "invalid", "annual")
        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_empty_data_returns_empty_list(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"data": []})):
            results = provider.get_financials("AAPL", "income", "annual")
        assert results == []


# -----------------------------------------------------------------------
# get_ratios
# -----------------------------------------------------------------------


class TestGetRatios:
    _metric_data = {
        "metric": {
            "peAnnual": 29.5,
            "pbAnnual": 47.3,
            "psTTM": 7.8,
            "roeTTM": 1.56,
            "roaRfy": 0.28,
            "grossMarginTTM": 0.443,
            "operatingMarginTTM": 0.298,
            "netProfitMarginTTM": 0.253,
            "dividendYieldIndicatedAnnual": 0.0051,
            "currentRatioAnnual": 0.99,
            "totalDebt/totalEquityAnnual": 1.79,
        }
    }

    def test_returns_ratios(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._metric_data)):
            results = provider.get_ratios("AAPL", "annual")
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, FinancialRatios)
        assert r.pe_ratio == 29.5
        assert r.pb_ratio == 47.3
        assert r.source == "finnhub"

    def test_empty_metric_returns_empty(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"metric": {}})):
            results = provider.get_ratios("AAPL", "annual")
        assert results == []


# -----------------------------------------------------------------------
# get_earnings
# -----------------------------------------------------------------------


class TestGetEarnings:
    _earnings_data = [
        {
            "actual": 2.18,
            "estimate": 2.10,
            "period": "2024-03-31",
            "quarter": 2,
            "surprise": 0.08,
            "surprisePercent": 3.81,
            "year": 2024,
        },
        {
            "actual": 2.46,
            "estimate": 2.35,
            "period": "2023-12-31",
            "quarter": 1,
            "surprise": 0.11,
            "surprisePercent": 4.68,
            "year": 2024,
        },
    ]

    def test_returns_earnings(self, provider: FinnhubProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._earnings_data)
        ):
            results = provider.get_earnings("AAPL")
        assert len(results) == 2
        assert all(isinstance(r, EarningsRecord) for r in results)
        assert results[0].eps_actual == 2.18
        assert results[0].eps_estimate == 2.10
        assert results[0].eps_surprise == 0.08
        assert results[0].period == "2024-Q2"
        assert results[0].source == "finnhub"

    def test_empty_response_returns_empty(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            results = provider.get_earnings("AAPL")
        assert results == []

    def test_revenue_fields_are_none(self, provider: FinnhubProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._earnings_data)
        ):
            results = provider.get_earnings("AAPL")
        assert results[0].revenue_actual is None
        assert results[0].revenue_estimate is None


# -----------------------------------------------------------------------
# get_insider_trades
# -----------------------------------------------------------------------


class TestGetInsiderTrades:
    _trades_data = {
        "data": [
            {
                "change": -50000,
                "filingDate": "2024-01-15",
                "name": "Tim Cook",
                "share": 3000000,
                "source": "A",
                "symbol": "AAPL",
                "transactionDate": "2024-01-12",
                "transactionPrice": 185.64,
                "transactionCode": "S",
            },
            {
                "change": 10000,
                "filingDate": "2024-01-10",
                "name": "Luca Maestri",
                "share": 500000,
                "source": "O",
                "symbol": "AAPL",
                "transactionDate": "2024-01-09",
                "transactionPrice": 183.00,
                "transactionCode": "P",
            },
        ]
    }

    def test_returns_trades(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._trades_data)):
            results = provider.get_insider_trades("AAPL")
        assert len(results) == 2
        assert all(isinstance(r, InsiderTrade) for r in results)
        assert results[0].insider_name == "Tim Cook"
        assert results[0].trade_type == "sell"
        assert results[1].trade_type == "buy"
        assert results[0].source == "finnhub"

    def test_since_filter(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._trades_data)):
            results = provider.get_insider_trades("AAPL", since=date(2024, 1, 12))
        assert len(results) == 1
        assert results[0].insider_name == "Tim Cook"

    def test_empty_data_returns_empty(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"data": []})):
            results = provider.get_insider_trades("AAPL")
        assert results == []


# -----------------------------------------------------------------------
# _xbrl_float / _xbrl_float_opt helpers
# -----------------------------------------------------------------------


class TestXbrlHelpers:
    def test_xbrl_float_returns_first_match(self) -> None:
        vals = {"us-gaap:Revenues": "100.5"}
        assert _xbrl_float(vals, ["us-gaap:Revenues"]) == 100.5

    def test_xbrl_float_skips_unconvertible(self) -> None:
        vals = {"us-gaap:Revenues": "bad", "us-gaap:SalesRevenueNet": "200.0"}
        assert _xbrl_float(vals, ["us-gaap:Revenues", "us-gaap:SalesRevenueNet"]) == 200.0

    def test_xbrl_float_returns_zero_when_no_match(self) -> None:
        assert _xbrl_float({}, ["us-gaap:NoMatch"]) == 0.0

    def test_xbrl_float_opt_returns_none_when_no_match(self) -> None:
        assert _xbrl_float_opt({}, ["us-gaap:NoMatch"]) is None

    def test_xbrl_float_opt_skips_unconvertible(self) -> None:
        vals = {"us-gaap:Assets": "bad"}
        assert _xbrl_float_opt(vals, ["us-gaap:Assets"]) is None

    def test_xbrl_float_opt_returns_value(self) -> None:
        vals = {"us-gaap:Assets": "500.0"}
        assert _xbrl_float_opt(vals, ["us-gaap:Assets"]) == 500.0


# -----------------------------------------------------------------------
# get_news
# -----------------------------------------------------------------------


class TestGetNews:
    _news_data = [
        {
            "headline": "Apple Reports Record Revenue",
            "source": "Reuters",
            "url": "https://reuters.com/apple",
            "datetime": 1704196800,
            "summary": "Apple beat Q1 estimates...",
        },
        {
            "headline": "Apple Unveils New Product",
            "source": "Bloomberg",
            "url": "https://bloomberg.com/apple",
            "datetime": 1704110400,
            "summary": "Apple announced...",
        },
    ]

    def test_returns_articles(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._news_data)):
            articles = provider.get_news("AAPL")
        assert len(articles) == 2
        assert all(isinstance(a, NewsArticle) for a in articles)
        assert articles[0].title == "Apple Reports Record Revenue"
        assert articles[0].publisher == "Reuters"
        assert articles[0].source == "finnhub"

    def test_empty_returns_empty(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            articles = provider.get_news("AAPL")
        assert articles == []

    def test_non_list_returns_empty(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({})):
            articles = provider.get_news("AAPL")
        assert articles == []

    def test_limit_respected(self, provider: FinnhubProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._news_data * 3)
        ):
            articles = provider.get_news("AAPL", limit=3)
        assert len(articles) == 3


# -----------------------------------------------------------------------
# get_analyst_data
# -----------------------------------------------------------------------


class TestGetAnalystData:
    _pt_data = {
        "targetHigh": 220.0,
        "targetLow": 160.0,
        "targetMean": 195.0,
        "targetMedian": 197.0,
    }
    _rec_data = [{"buy": 25, "hold": 10, "sell": 3, "strongBuy": 15, "strongSell": 1}]

    def test_returns_analyst_data(self, provider: FinnhubProvider) -> None:
        responses = [_mock_response(self._pt_data), _mock_response(self._rec_data)]
        with patch.object(provider._client, "get", side_effect=responses):
            data = provider.get_analyst_data("AAPL")
        assert isinstance(data, AnalystData)
        assert data.target_high == 220.0
        assert data.target_mean == 195.0
        assert data.rating_buy == 25
        assert data.source == "finnhub"

    def test_empty_both_raises(self, provider: FinnhubProvider) -> None:
        responses = [_mock_response({}), _mock_response([])]
        with patch.object(provider._client, "get", side_effect=responses):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_analyst_data("AAPL")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_recommendation_list_used(self, provider: FinnhubProvider) -> None:
        responses = [_mock_response({}), _mock_response(self._rec_data)]
        with patch.object(provider._client, "get", side_effect=responses):
            data = provider.get_analyst_data("AAPL")
        assert data.rating_buy == 25


# -----------------------------------------------------------------------
# get_forward_estimates
# -----------------------------------------------------------------------


class TestGetForwardEstimates:
    _rev_data = {
        "data": [
            {"period": "2024-12-31", "revenueAvg": 400_000_000_000},
            {"period": "2025-12-31", "revenueAvg": 430_000_000_000},
        ]
    }
    _eps_data = {
        "data": [
            {"period": "2024-12-31", "epsAvg": 6.80},
            {"period": "2025-12-31", "epsAvg": 7.50},
        ]
    }

    def test_returns_estimates(self, provider: FinnhubProvider) -> None:
        responses = [_mock_response(self._rev_data), _mock_response(self._eps_data)]
        with patch.object(provider._client, "get", side_effect=responses):
            results = provider.get_forward_estimates("AAPL")
        assert len(results) == 2
        assert all(isinstance(r, ForwardEstimates) for r in results)
        assert results[0].source == "finnhub"

    def test_empty_returns_empty(self, provider: FinnhubProvider) -> None:
        responses = [_mock_response({}), _mock_response({})]
        with patch.object(provider._client, "get", side_effect=responses):
            results = provider.get_forward_estimates("AAPL")
        assert results == []

    def test_period_without_period_key_skipped(self, provider: FinnhubProvider) -> None:
        rev_data = {"data": [{"revenueAvg": 400e9}]}  # no "period" key
        responses = [_mock_response(rev_data), _mock_response({})]
        with patch.object(provider._client, "get", side_effect=responses):
            results = provider.get_forward_estimates("AAPL")
        assert results == []


# -----------------------------------------------------------------------
# is_rate_limited / cooldown_for
# -----------------------------------------------------------------------


class TestIsRateLimited:
    def test_429_http_response_is_rate_limited(self, provider: FinnhubProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 429
        assert provider.is_rate_limited(resp) is True

    def test_200_http_response_not_rate_limited(self, provider: FinnhubProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        assert provider.is_rate_limited(resp) is False

    def test_exception_with_429_string(self, provider: FinnhubProvider) -> None:
        exc = Exception("HTTP 429 Too Many Requests")
        assert provider.is_rate_limited(exc) is True

    def test_non_exception_not_rate_limited(self, provider: FinnhubProvider) -> None:
        assert provider.is_rate_limited("some_data") is False

    def test_cooldown_uses_retry_after_header(self, provider: FinnhubProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.headers = {"Retry-After": "120"}
        assert provider.cooldown_for(resp) == 120.0

    def test_cooldown_default_when_no_header(self, provider: FinnhubProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.headers = {}
        assert provider.cooldown_for(resp) == 60.0

    def test_cooldown_non_response_default(self, provider: FinnhubProvider) -> None:
        assert provider.cooldown_for(None) == 60.0

    def test_cooldown_bad_retry_after_header_returns_default(
        self, provider: FinnhubProvider
    ) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.headers = {"Retry-After": "not-a-number"}
        assert provider.cooldown_for(resp) == 60.0


# -----------------------------------------------------------------------
# _get helper — non-200 response
# -----------------------------------------------------------------------


class TestGetHelper:
    def test_non_200_raises_provider_error(self, provider: FinnhubProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 403
        resp.text = "Forbidden"
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError) as exc_info:
                provider._get("stock/metric")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_financials — balance sheet and cashflow
# -----------------------------------------------------------------------


class TestGetFinancialsBalanceAndCash:
    _xbrl_data = {
        "data": [
            {
                "endDate": "2023-09-30",
                "year": 2023,
                "quarter": 0,
                "report": {
                    "bs": [
                        {"concept": "us-gaap:Assets", "value": 352_755_000_000},
                        {"concept": "us-gaap:Liabilities", "value": 290_455_000_000},
                        {
                            "concept": "us-gaap:StockholdersEquity",
                            "value": 62_146_000_000,
                        },
                        {
                            "concept": "us-gaap:CashAndCashEquivalentsAtCarryingValue",
                            "value": 29_965_000_000,
                        },
                        {"concept": "us-gaap:LongTermDebt", "value": 95_281_000_000},
                    ],
                    "ic": [],
                    "cf": [],
                },
            }
        ]
    }

    def test_returns_balance_sheet(self, provider: FinnhubProvider) -> None:
        from onefinance.core.models import BalanceSheet

        with patch.object(provider._client, "get", return_value=_mock_response(self._xbrl_data)):
            results = provider.get_financials("AAPL", statement="balance", period="annual")
        assert len(results) == 1
        assert isinstance(results[0], BalanceSheet)
        assert results[0].total_assets == 352_755_000_000

    def test_returns_cashflow(self, provider: FinnhubProvider) -> None:
        from onefinance.core.models import CashFlow

        cf_data = {
            "data": [
                {
                    "endDate": "2023-09-30",
                    "year": 2023,
                    "quarter": 0,
                    "report": {
                        "bs": [],
                        "ic": [],
                        "cf": [
                            {
                                "concept": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                                "value": 110_543_000_000,
                            },
                            {
                                "concept": "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                                "value": 10_959_000_000,
                            },
                            {"concept": "us-gaap:PaymentsOfDividends", "value": 14_992_000_000},
                        ],
                    },
                }
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(cf_data)):
            results = provider.get_financials("AAPL", statement="cashflow", period="annual")
        assert len(results) == 1
        assert isinstance(results[0], CashFlow)
        assert results[0].operating_cash_flow == 110_543_000_000

    def test_invalid_end_date_skipped(self, provider: FinnhubProvider) -> None:
        bad_data = {
            "data": [
                {
                    "endDate": "not-a-date",
                    "year": 2023,
                    "quarter": 1,
                    "report": {"bs": [], "ic": [], "cf": []},
                }
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(bad_data)):
            results = provider.get_financials("AAPL", statement="balance", period="annual")
        assert results == []


# -----------------------------------------------------------------------
# get_earnings / get_insider_trades edge cases
# -----------------------------------------------------------------------


class TestGetEarningsEdgeCases:
    def test_invalid_period_date_skipped(self, provider: FinnhubProvider) -> None:
        data = [{"period": "not-a-date", "actual": 2.18, "estimate": 2.10}]
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_earnings("AAPL")
        assert results == []


class TestGetInsiderTradesEdgeCases:
    def test_missing_filing_date_skipped(self, provider: FinnhubProvider) -> None:
        data = {
            "data": [
                {"name": "Tim Cook", "change": -50000, "transactionCode": "S"},
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_insider_trades("AAPL")
        assert results == []

    def test_invalid_filing_date_skipped(self, provider: FinnhubProvider) -> None:
        data = {
            "data": [
                {
                    "filingDate": "not-a-date",
                    "name": "Tim Cook",
                    "change": -50000,
                    "transactionCode": "S",
                }
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_insider_trades("AAPL")
        assert results == []

    def test_exercise_trade_type(self, provider: FinnhubProvider) -> None:
        data = {
            "data": [
                {
                    "filingDate": "2024-01-15",
                    "transactionDate": "2024-01-12",
                    "name": "Tim Cook",
                    "change": 10000,
                    "transactionCode": "M",
                    "transactionPrice": 150.0,
                    "share": 300000,
                    "source": "A",
                }
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_insider_trades("AAPL")
        assert len(results) == 1
        assert results[0].trade_type == "exercise"


# -----------------------------------------------------------------------
# get_forward_estimates — eps data period without key
# -----------------------------------------------------------------------


class TestGetForwardEstimatesEdgeCases:
    def test_eps_without_period_skipped(self, provider: FinnhubProvider) -> None:
        rev_data: dict[str, object] = {}
        eps_data = {"data": [{"epsAvg": 6.80}]}  # no "period" key
        responses = [_mock_response(rev_data), _mock_response(eps_data)]
        with patch.object(provider._client, "get", side_effect=responses):
            results = provider.get_forward_estimates("AAPL")
        assert results == []


# -----------------------------------------------------------------------
# get_earnings_calendar
# -----------------------------------------------------------------------


class TestGetEarningsCalendar:
    _calendar_payload = {
        "earningsCalendar": [
            {
                "date": "2026-06-05",
                "epsActual": None,
                "epsEstimate": 1.40,
                "hour": "amc",
                "quarter": 3,
                "revenueActual": None,
                "revenueEstimate": 94500000000.0,
                "symbol": "AAPL",
                "year": 2026,
            },
            {
                "date": "2026-06-04",
                "epsActual": 3.12,
                "epsEstimate": 3.10,
                "hour": "bmo",
                "quarter": 2,
                "revenueActual": 70100000000.0,
                "revenueEstimate": 70000000000.0,
                "symbol": "MSFT",
                "year": 2026,
            },
        ]
    }

    def test_returns_entries(self, provider: FinnhubProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._calendar_payload)
        ):
            results = provider.get_earnings_calendar()
        assert len(results) == 2
        assert all(isinstance(r, EarningsCalendarEntry) for r in results)

    def test_field_mapping(self, provider: FinnhubProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._calendar_payload)
        ):
            results = provider.get_earnings_calendar()
        aapl = next(r for r in results if r.symbol == "AAPL")
        assert aapl.report_date == date(2026, 6, 5)
        assert aapl.eps_estimate == 1.40
        assert aapl.eps_actual is None
        assert aapl.time_of_day == "amc"
        assert aapl.quarter == 3
        assert aapl.year == 2026
        assert aapl.source == "finnhub"

    def test_bmo_time_of_day(self, provider: FinnhubProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._calendar_payload)
        ):
            results = provider.get_earnings_calendar()
        msft = next(r for r in results if r.symbol == "MSFT")
        assert msft.time_of_day == "bmo"

    def test_empty_hour_maps_to_none(self, provider: FinnhubProvider) -> None:
        payload = {
            "earningsCalendar": [
                {"symbol": "XYZ", "date": "2026-06-05", "hour": "", "quarter": 1, "year": 2026}
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            results = provider.get_earnings_calendar()
        assert results[0].time_of_day is None

    def test_date_range_passed(self, provider: FinnhubProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._calendar_payload)
        ) as mock_get:
            provider.get_earnings_calendar(start=date(2026, 6, 1), end=date(2026, 6, 7))
        call_kwargs = mock_get.call_args
        params = call_kwargs[1].get("params", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {})
        assert "from" in params or "from" in str(call_kwargs)

    def test_empty_calendar_returns_empty(self, provider: FinnhubProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response({"earningsCalendar": []})
        ):
            results = provider.get_earnings_calendar()
        assert results == []

    def test_non_dict_response_returns_empty(self, provider: FinnhubProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            results = provider.get_earnings_calendar()
        assert results == []

    def test_skips_entry_without_symbol(self, provider: FinnhubProvider) -> None:
        payload = {
            "earningsCalendar": [
                {"date": "2026-06-05", "hour": "amc"},
                {"symbol": "AAPL", "date": "2026-06-05", "hour": "amc"},
            ]
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            results = provider.get_earnings_calendar()
        assert len(results) == 1
        assert results[0].symbol == "AAPL"

    def test_supports_earnings_calendar_endpoint(self, provider: FinnhubProvider) -> None:
        assert provider.supports("earnings_calendar") is True
