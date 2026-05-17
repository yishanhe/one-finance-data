"""Unit tests for FMPProvider with mocked httpx responses."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from onefinance.core.errors import ConfigError, ProviderError, RateLimitError
from onefinance.core.models import (
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    EarningsRecord,
    FinancialRatios,
    IncomeStatement,
    InsiderTrade,
    PriceBar,
    Quote,
)
from onefinance.providers.fmp import FMPProvider


@pytest.fixture
def provider() -> FMPProvider:
    return FMPProvider(api_key="test_key", timeout=5)


def _mock_response(
    data: Any,
    status_code: int = 200,
) -> httpx.Response:
    """Create a mock httpx.Response."""
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
        p = FMPProvider(api_key="my_key")
        assert p._api_key == "my_key"

    def test_api_key_from_env(self) -> None:
        with patch.dict("os.environ", {"FMP_API_KEY": "env_key"}):
            p = FMPProvider()
            assert p._api_key == "env_key"

    def test_missing_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ConfigError):
                FMPProvider()


# -----------------------------------------------------------------------
# get_price_history
# -----------------------------------------------------------------------


class TestGetPriceHistory:
    def test_returns_bars(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "symbol": "AAPL",
                "date": "2024-01-03",
                "open": 184.22,
                "high": 185.88,
                "low": 183.43,
                "close": 184.25,
                "volume": 58414500,
            },
            {
                "symbol": "AAPL",
                "date": "2024-01-02",
                "open": 187.15,
                "high": 188.44,
                "low": 183.89,
                "close": 185.64,
                "volume": 82488700,
            },
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 4))

        assert len(bars) == 2
        assert all(isinstance(b, PriceBar) for b in bars)
        # Should be chronological (reversed from FMP's newest-first)
        assert bars[0].date == date(2024, 1, 2)
        assert bars[1].date == date(2024, 1, 3)
        assert bars[0].source == "fmp"

    def test_empty_response(self, provider: FMPProvider) -> None:
        resp = _mock_response([])
        with patch.object(provider._client, "get", return_value=resp):
            bars = provider.get_price_history("INVALID", date(2024, 1, 1), date(2024, 1, 2))
        assert bars == []


# -----------------------------------------------------------------------
# get_quote
# -----------------------------------------------------------------------


class TestGetQuote:
    def test_returns_quote(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "price": 298.87,
                "volume": 45300552,
                "timestamp": 1778702401,
            }
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            quote = provider.get_quote("AAPL")

        assert isinstance(quote, Quote)
        assert quote.symbol == "AAPL"
        assert quote.price == 298.87
        assert quote.source == "fmp"

    def test_empty_raises(self, provider: FMPProvider) -> None:
        resp = _mock_response([])
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("INVALID")
            assert exc_info.value.code == "SYMBOL_NOT_FOUND"


# -----------------------------------------------------------------------
# get_info
# -----------------------------------------------------------------------


class TestGetInfo:
    def test_returns_info(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "symbol": "AAPL",
                "companyName": "Apple Inc.",
                "exchange": "NASDAQ",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "country": "US",
                "marketCap": 4389610087720,
                "description": "Apple designs things.",
                "website": "https://apple.com",
                "fullTimeEmployees": 164000,
                "currency": "USD",
            }
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            info = provider.get_info("AAPL")

        assert isinstance(info, CompanyInfo)
        assert info.name == "Apple Inc."
        assert info.sector == "Technology"
        assert info.market_cap == 4389610087720.0
        assert info.employees == 164000
        assert info.currency == "USD"

    def test_empty_raises(self, provider: FMPProvider) -> None:
        resp = _mock_response([])
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError):
                provider.get_info("INVALID")


# -----------------------------------------------------------------------
# get_financials
# -----------------------------------------------------------------------


class TestGetFinancials:
    def test_income_statement(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "date": "2025-09-27",
                "symbol": "AAPL",
                "reportedCurrency": "USD",
                "fiscalYear": "2025",
                "period": "FY",
                "revenue": 416161000000,
                "costOfRevenue": 220960000000,
                "grossProfit": 195201000000,
                "operatingIncome": 133050000000,
                "netIncome": 112010000000,
                "eps": 7.36,
                "epsDiluted": 7.34,
            }
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            stmts = provider.get_financials("AAPL", "income", "annual")

        assert len(stmts) == 1
        assert isinstance(stmts[0], IncomeStatement)
        assert stmts[0].revenue == 416161000000
        assert stmts[0].period == "2025-FY"
        assert stmts[0].currency == "USD"

    def test_balance_sheet(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "date": "2025-09-27",
                "symbol": "AAPL",
                "reportedCurrency": "USD",
                "fiscalYear": "2025",
                "period": "FY",
                "totalAssets": 359500000000,
                "totalLiabilities": 265000000000,
                "totalStockholdersEquity": 94500000000,
                "cashAndCashEquivalents": 35934000000,
                "totalDebt": 96000000000,
            }
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            stmts = provider.get_financials("AAPL", "balance", "annual")

        assert len(stmts) == 1
        assert isinstance(stmts[0], BalanceSheet)
        assert stmts[0].total_assets == 359500000000

    def test_cashflow(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "date": "2025-09-27",
                "symbol": "AAPL",
                "reportedCurrency": "USD",
                "fiscalYear": "2025",
                "period": "FY",
                "operatingCashFlow": 118254000000,
                "capitalExpenditure": -9959000000,
                "freeCashFlow": 108295000000,
                "dividendsPaid": -15892000000,
            }
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            stmts = provider.get_financials("AAPL", "cashflow", "annual")

        assert len(stmts) == 1
        assert isinstance(stmts[0], CashFlow)
        assert stmts[0].free_cash_flow == 108295000000

    def test_unknown_statement_raises(self, provider: FMPProvider) -> None:
        with pytest.raises(ProviderError):
            provider.get_financials("AAPL", "unknown", "annual")


# -----------------------------------------------------------------------
# get_ratios
# -----------------------------------------------------------------------


class TestGetRatios:
    def test_returns_ratios(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "symbol": "AAPL",
                "date": "2025-09-27",
                "fiscalYear": "2025",
                "period": "FY",
                "reportedCurrency": "USD",
                "grossProfitMargin": 0.469,
                "operatingProfitMargin": 0.319,
                "netProfitMargin": 0.269,
                "returnOnEquity": 1.36,
                "returnOnAssets": 0.31,
                "currentRatio": 0.89,
                "debtEquityRatio": 1.45,
                "priceEarningsRatio": 35.2,
                "priceToBookRatio": 48.5,
                "priceToSalesRatio": 9.8,
                "dividendYield": 0.004,
            }
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            ratios = provider.get_ratios("AAPL", "annual")

        assert len(ratios) == 1
        assert isinstance(ratios[0], FinancialRatios)
        assert ratios[0].pe_ratio == 35.2
        assert ratios[0].gross_margin == 0.469
        assert ratios[0].period == "2025-FY"


# -----------------------------------------------------------------------
# get_earnings
# -----------------------------------------------------------------------


class TestGetEarnings:
    def test_returns_earnings(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "symbol": "AAPL",
                "date": "2026-04-30",
                "epsActual": 2.01,
                "epsEstimated": 1.95,
                "revenueActual": 111184000000,
                "revenueEstimated": 109457600000,
                "lastUpdated": "2026-05-01",
            },
            {
                "symbol": "AAPL",
                "date": "2026-01-30",
                "epsActual": 2.40,
                "epsEstimated": 2.35,
                "revenueActual": 124300000000,
                "revenueEstimated": 123500000000,
                "lastUpdated": "2026-01-31",
            },
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            earnings = provider.get_earnings("AAPL")

        assert len(earnings) == 2
        assert isinstance(earnings[0], EarningsRecord)
        assert earnings[0].eps_actual == 2.01
        assert earnings[0].eps_estimate == 1.95
        assert earnings[0].eps_surprise == 0.06  # 2.01 - 1.95

    def test_earnings_with_null_actuals(self, provider: FMPProvider) -> None:
        """Future earnings with no actuals yet."""
        mock_data = [
            {
                "symbol": "AAPL",
                "date": "2026-07-30",
                "epsActual": None,
                "epsEstimated": 1.86,
                "revenueActual": None,
                "revenueEstimated": 107946900000,
            }
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            earnings = provider.get_earnings("AAPL")

        assert len(earnings) == 1
        assert earnings[0].eps_actual is None
        assert earnings[0].eps_surprise is None
        assert earnings[0].eps_estimate == 1.86


# -----------------------------------------------------------------------
# get_insider_trades
# -----------------------------------------------------------------------


class TestGetInsiderTrades:
    def test_returns_trades(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "symbol": "AAPL",
                "filingDate": "2024-08-15",
                "transactionDate": "2024-08-14",
                "reportingName": "Tim Cook",
                "typeOfOwner": "officer: Chief Executive Officer",
                "transactionType": "S-Sale",
                "securitiesTransacted": 50000,
                "price": 225.0,
                "value": 11250000,
                "securitiesOwned": 100000,
            }
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            trades = provider.get_insider_trades("AAPL")

        assert len(trades) == 1
        assert isinstance(trades[0], InsiderTrade)
        assert trades[0].insider_name == "Tim Cook"
        assert trades[0].trade_type == "sell"
        assert trades[0].shares == 50000.0

    def test_since_filter(self, provider: FMPProvider) -> None:
        """Trades before `since` should be filtered out."""
        mock_data = [
            {
                "symbol": "AAPL",
                "filingDate": "2024-08-15",
                "transactionDate": "2024-08-14",
                "reportingName": "A",
                "transactionType": "S-Sale",
                "securitiesTransacted": 100,
            },
            {
                "symbol": "AAPL",
                "filingDate": "2024-01-10",
                "transactionDate": "2024-01-09",
                "reportingName": "B",
                "transactionType": "P-Purchase",
                "securitiesTransacted": 200,
            },
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            trades = provider.get_insider_trades("AAPL", since=date(2024, 6, 1))

        assert len(trades) == 1
        assert trades[0].insider_name == "A"

    def test_empty_response(self, provider: FMPProvider) -> None:
        resp = _mock_response([])
        with patch.object(provider._client, "get", return_value=resp):
            trades = provider.get_insider_trades("AAPL")
        assert trades == []


# -----------------------------------------------------------------------
# Rate-limit detection
# -----------------------------------------------------------------------


class TestRateLimitDetection:
    def test_http_429(self, provider: FMPProvider) -> None:
        resp = _mock_response("Rate limited", status_code=429)
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(RateLimitError):
                provider.get_quote("AAPL")

    def test_limit_reach_in_body(self, provider: FMPProvider) -> None:
        resp = _mock_response(
            {"Error Message": "Limit Reach. Please upgrade your plan."},
            status_code=200,
        )
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(RateLimitError):
                provider.get_quote("AAPL")

    def test_is_rate_limited_on_429_response(self, provider: FMPProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 429
        assert provider.is_rate_limited(resp) is True

    def test_is_rate_limited_on_normal_response(self, provider: FMPProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.text = '{"data": []}'
        assert provider.is_rate_limited(resp) is False

    def test_cooldown_1_hour(self, provider: FMPProvider) -> None:
        assert provider.cooldown_for(None) == 3600.0


# -----------------------------------------------------------------------
# Capability discovery
# -----------------------------------------------------------------------


class TestFMPCapabilities:
    def test_supports_all_endpoints(self, provider: FMPProvider) -> None:
        for ep in [
            "price_history",
            "quote",
            "info",
            "financials",
            "ratios",
            "earnings",
            "insider_trades",
        ]:
            assert provider.supports(ep) is True, f"Should support {ep}"

    def test_supported_endpoints_list(self, provider: FMPProvider) -> None:
        endpoints = provider.supported_endpoints
        assert len(endpoints) == 14


# -----------------------------------------------------------------------
# Network errors
# -----------------------------------------------------------------------


class TestNetworkErrors:
    def test_http_error_raises_provider_error(self, provider: FMPProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("AAPL")
            assert exc_info.value.code == "NETWORK_ERROR"
            assert exc_info.value.retry_safe is True

    def test_5xx_is_retryable(self, provider: FMPProvider) -> None:
        resp = _mock_response("Internal Server Error", status_code=500)
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("AAPL")
            assert exc_info.value.retry_safe is True
