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
    CompanyInfo,
    EarningsRecord,
    FinancialRatios,
    IncomeStatement,
    InsiderTrade,
    PriceBar,
    Quote,
)
from onefinance.providers.finnhub import FinnhubProvider


@pytest.fixture
def provider() -> FinnhubProvider:
    return FinnhubProvider(api_key="test_key", timeout=5)


def _mock_response(
    data: Any,
    status_code: int = 200,
    headers: dict | None = None,
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
    def test_api_key_from_param(self):
        p = FinnhubProvider(api_key="my_key")
        assert p._api_key == "my_key"

    def test_api_key_from_env(self):
        with patch.dict("os.environ", {"FINNHUB_API_KEY": "env_key"}):
            p = FinnhubProvider()
            assert p._api_key == "env_key"

    def test_missing_key_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ConfigError):
                FinnhubProvider()

    def test_name(self):
        p = FinnhubProvider(api_key="k")
        assert p.name == "finnhub"


# -----------------------------------------------------------------------
# Rate-limit detection
# -----------------------------------------------------------------------


class TestRateLimitDetection:
    def test_http_429_raises_rate_limit(self, provider):
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({}, 429, {"Retry-After": "120"}),
        ):
            with pytest.raises(RateLimitError) as exc_info:
                provider._get("quote", {"symbol": "AAPL"})
        assert exc_info.value.retry_after_seconds == 120

    def test_http_429_default_cooldown(self, provider):
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({}, 429, {}),
        ):
            with pytest.raises(RateLimitError) as exc_info:
                provider._get("quote", {})
        assert exc_info.value.retry_after_seconds == 60

    def test_non_200_raises_provider_error(self, provider):
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

    def test_returns_price_bars(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response(self._candle_data)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))
        assert len(bars) == 2
        assert all(isinstance(b, PriceBar) for b in bars)
        assert bars[0].symbol == "AAPL"
        assert bars[0].close == 185.64
        assert bars[0].volume == 52000000
        assert bars[0].source == "finnhub"

    def test_no_data_returns_empty(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response({"s": "no_data"})):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))
        assert bars == []

    def test_symbol_uppercased(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response(self._candle_data)):
            bars = provider.get_price_history("aapl", date(2024, 1, 2), date(2024, 1, 3))
        assert bars[0].symbol == "AAPL"

    def test_adj_close_equals_close(self, provider):
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

    def test_returns_quote(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response(self._quote_data)):
            q = provider.get_quote("AAPL")
        assert isinstance(q, Quote)
        assert q.symbol == "AAPL"
        assert q.price == 185.64
        assert q.source == "finnhub"

    def test_no_data_raises(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response({"c": None})):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("AAPL")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_symbol_uppercased(self, provider):
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

    def test_returns_company_info(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response(self._profile_data)):
            info = provider.get_info("AAPL")
        assert isinstance(info, CompanyInfo)
        assert info.name == "Apple Inc"
        assert info.industry == "Technology"
        assert info.country == "US"
        assert info.currency == "USD"
        assert info.source == "finnhub"

    def test_market_cap_converted_from_millions(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response(self._profile_data)):
            info = provider.get_info("AAPL")
        assert info.market_cap == 2_900_000.0 * 1_000_000

    def test_empty_response_raises(self, provider):
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

    def test_income_statement(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response(self._income_data)):
            results = provider.get_financials("AAPL", "income", "annual")
        assert len(results) == 1
        stmt = results[0]
        assert isinstance(stmt, IncomeStatement)
        assert stmt.revenue == 383285000000
        assert stmt.net_income == 96995000000
        assert stmt.period == "2023-FY"
        assert stmt.source == "finnhub"

    def test_invalid_statement_raises(self, provider):
        with pytest.raises(ProviderError) as exc_info:
            provider.get_financials("AAPL", "invalid", "annual")
        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_empty_data_returns_empty_list(self, provider):
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

    def test_returns_ratios(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response(self._metric_data)):
            results = provider.get_ratios("AAPL", "annual")
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, FinancialRatios)
        assert r.pe_ratio == 29.5
        assert r.pb_ratio == 47.3
        assert r.source == "finnhub"

    def test_empty_metric_returns_empty(self, provider):
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

    def test_returns_earnings(self, provider):
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

    def test_empty_response_returns_empty(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            results = provider.get_earnings("AAPL")
        assert results == []

    def test_revenue_fields_are_none(self, provider):
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

    def test_returns_trades(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response(self._trades_data)):
            results = provider.get_insider_trades("AAPL")
        assert len(results) == 2
        assert all(isinstance(r, InsiderTrade) for r in results)
        assert results[0].insider_name == "Tim Cook"
        assert results[0].trade_type == "sell"
        assert results[1].trade_type == "buy"
        assert results[0].source == "finnhub"

    def test_since_filter(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response(self._trades_data)):
            results = provider.get_insider_trades("AAPL", since=date(2024, 1, 12))
        assert len(results) == 1
        assert results[0].insider_name == "Tim Cook"

    def test_empty_data_returns_empty(self, provider):
        with patch.object(provider._client, "get", return_value=_mock_response({"data": []})):
            results = provider.get_insider_trades("AAPL")
        assert results == []
