"""Unit tests for FMPProvider with mocked httpx responses."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from onefinance.core.errors import ConfigError, NotSupportedError, ProviderError, RateLimitError
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    DCFValuation,
    EarningsCalendarEntry,
    EarningsRecord,
    EconomicEvent,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    PriceBar,
    Quote,
    ScreenerResult,
    ShortInterest,
    TreasuryRate,
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
                "marketCap": 4_500_000_000_000,
                "previousClose": 295.00,
            }
        ]
        resp = _mock_response(mock_data)

        with patch.object(provider._client, "get", return_value=resp):
            quote = provider.get_quote("AAPL")

        assert isinstance(quote, Quote)
        assert quote.symbol == "AAPL"
        assert quote.price == 298.87
        assert quote.market_cap == 4_500_000_000_000.0
        assert quote.source == "fmp"
        assert quote.prev_close == 295.00
        assert quote.change_pct == pytest.approx((298.87 - 295.00) / 295.00 * 100, rel=1e-4)

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
            "treasury_rates",
        ]:
            assert provider.supports(ep) is True, f"Should support {ep}"

    def test_supported_endpoints_list(self, provider: FMPProvider) -> None:
        endpoints = provider.supported_endpoints
        assert "quote" in endpoints
        assert "quotes" in endpoints
        assert "info" in endpoints
        assert "infos" in endpoints
        assert "institutional_holders" in endpoints
        assert "treasury_rates" in endpoints


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

    def test_402_raises_not_supported(self, provider: FMPProvider) -> None:
        resp = _mock_response("Premium Query Parameter: upgrade required", status_code=402)
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(NotSupportedError) as exc_info:
                provider.get_quote("AAPL")
        assert exc_info.value.code == "NOT_SUPPORTED"
        assert exc_info.value.provider == "fmp"

    def test_5xx_is_retryable(self, provider: FMPProvider) -> None:
        resp = _mock_response("Internal Server Error", status_code=500)
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("AAPL")
            assert exc_info.value.retry_safe is True

    def test_error_message_in_json_raises(self, provider: FMPProvider) -> None:
        resp = _mock_response({"Error Message": "Invalid API key"})
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("AAPL")
        assert exc_info.value.code == "NETWORK_ERROR"


# -----------------------------------------------------------------------
# get_dcf
# -----------------------------------------------------------------------


class TestGetDcf:
    _dcf_data = [{"dcf": 195.50, "stockPrice": 185.64, "date": "2024-01-03"}]

    def test_returns_dcf(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._dcf_data)):
            result = provider.get_dcf("AAPL")
        assert isinstance(result, DCFValuation)
        assert result.symbol == "AAPL"
        assert result.dcf == 195.50
        assert result.stock_price == 185.64
        assert result.source == "fmp"

    def test_empty_data_raises(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_dcf("AAPL")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_missing_fields_raises(self, provider: FMPProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response([{"date": "2024-01-03"}])
        ):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_dcf("AAPL")
        assert exc_info.value.code == "SCHEMA_DRIFT"

    def test_dict_response(self, provider: FMPProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({"dcf": 195.50, "stockPrice": 185.64}),
        ):
            result = provider.get_dcf("AAPL")
        assert result.dcf == 195.50


# -----------------------------------------------------------------------
# get_news
# -----------------------------------------------------------------------


class TestGetNews:
    def test_raises_not_supported(self, provider: FMPProvider) -> None:
        with pytest.raises(NotSupportedError):
            provider.get_news("AAPL")


# -----------------------------------------------------------------------
# get_corporate_actions
# -----------------------------------------------------------------------


class TestGetCorporateActions:
    _div_data = {
        "historical": [
            {"date": "2024-02-09", "adjDividend": 0.24, "dividend": 0.24},
            {"date": "2023-11-10", "adjDividend": 0.24, "dividend": 0.24},
        ]
    }
    _split_data = {
        "historical": [
            {"date": "2020-08-31", "numerator": 4.0, "denominator": 1.0},
        ]
    }

    def test_returns_dividends_and_splits(self, provider: FMPProvider) -> None:
        responses = [_mock_response(self._div_data), _mock_response(self._split_data)]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        assert len(actions) == 3
        types = {a.action_type for a in actions}
        assert "dividend" in types
        assert "split" in types

    def test_empty_returns_empty(self, provider: FMPProvider) -> None:
        responses = [_mock_response({}), _mock_response({})]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        assert actions == []

    def test_sorted_descending(self, provider: FMPProvider) -> None:
        responses = [_mock_response(self._div_data), _mock_response({})]
        with patch.object(provider._client, "get", side_effect=responses):
            actions = provider.get_corporate_actions("AAPL")
        assert actions[0].date >= actions[1].date


# -----------------------------------------------------------------------
# get_institutional_holders
# -----------------------------------------------------------------------


class TestGetInstitutionalHolders:
    _holders_data = [
        {
            "holder": "Vanguard Group",
            "shares": 1_200_000_000,
            "marketValue": 222_000_000_000,
            "change": 5_000_000,
            "changePercentage": 0.42,
        },
        {
            "holder": "BlackRock",
            "shares": 1_100_000_000,
            "marketValue": 203_000_000_000,
            "change": -2_000_000,
            "changePercentage": -0.18,
        },
    ]

    def test_returns_holders(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._holders_data)):
            holders = provider.get_institutional_holders("AAPL")
        assert len(holders) == 2
        assert all(isinstance(h, InstitutionalHolder) for h in holders)
        assert holders[0].holder_name == "Vanguard Group"
        assert holders[0].source == "fmp"

    def test_empty_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            holders = provider.get_institutional_holders("AAPL")
        assert holders == []

    def test_empty_data_envelope_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({"data": [], "rows": 0}),
        ):
            holders = provider.get_institutional_holders("AAPL")
        assert holders == []

    def test_error_dict_raises_provider_error(self, provider: FMPProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({"Error Message": "not in subscription"}),
        ):
            with pytest.raises(ProviderError, match="not in subscription"):
                provider.get_institutional_holders("AAPL")

    def test_malformed_dict_raises_provider_error(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({})):
            with pytest.raises(ProviderError, match="Unexpected FMP institutional holders"):
                provider.get_institutional_holders("AAPL")


# -----------------------------------------------------------------------
# get_analyst_data
# -----------------------------------------------------------------------


class TestGetAnalystData:
    _pt_data = [
        {
            "targetHigh": 220.0,
            "targetLow": 160.0,
            "targetConsensus": 195.0,
            "targetMedian": 197.0,
        }
    ]
    _rating_data = [
        {
            "analystRatingsBuy": 25,
            "analystRatingsHold": 10,
            "analystRatingsSell": 3,
            "analystRatingsStrongBuy": 15,
            "analystRatingsStrongSell": 1,
        }
    ]

    def test_returns_analyst_data(self, provider: FMPProvider) -> None:
        responses = [_mock_response(self._pt_data), _mock_response(self._rating_data)]
        with patch.object(provider._client, "get", side_effect=responses):
            data = provider.get_analyst_data("AAPL")
        assert isinstance(data, AnalystData)
        assert data.target_high == 220.0
        assert data.rating_buy == 25
        assert data.source == "fmp"

    def test_empty_both_raises(self, provider: FMPProvider) -> None:
        responses = [_mock_response([]), _mock_response([])]
        with patch.object(provider._client, "get", side_effect=responses):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_analyst_data("AAPL")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"


# -----------------------------------------------------------------------
# screen_stocks
# -----------------------------------------------------------------------


class TestScreenStocks:
    _screener_data = [
        {
            "symbol": "AAPL",
            "companyName": "Apple Inc.",
            "marketCap": 3_000_000_000_000,
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "price": 185.64,
            "volume": 52_000_000,
        },
        {
            "symbol": "MSFT",
            "companyName": "Microsoft Corporation",
            "marketCap": 2_800_000_000_000,
            "sector": "Technology",
            "industry": "Software",
            "price": 374.51,
            "volume": 25_000_000,
        },
    ]

    def test_returns_screener_results(self, provider: FMPProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._screener_data)
        ):
            results = provider.screen_stocks("sector=Technology&marketCapMoreThan=1000000000")
        assert len(results) == 2
        assert all(isinstance(r, ScreenerResult) for r in results)
        assert results[0].symbol == "AAPL"
        assert results[0].source == "fmp"

    def test_empty_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            results = provider.screen_stocks("sector=Nonexistent")
        assert results == []

    def test_non_list_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({})):
            results = provider.screen_stocks("sector=Technology")
        assert results == []


# -----------------------------------------------------------------------
# get_treasury_rates
# -----------------------------------------------------------------------


class TestGetTreasuryRates:
    _rates_data = [
        {
            "date": "2026-07-02",
            "month1": 4.32,
            "month3": 4.28,
            "month6": 4.18,
            "year1": 3.92,
            "year2": 3.75,
            "year5": 3.88,
            "year10": 4.12,
            "year30": 4.75,
        }
    ]

    def test_returns_treasury_rates(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._rates_data)):
            rates = provider.get_treasury_rates(date(2026, 7, 1), date(2026, 7, 2))

        assert len(rates) == 1
        assert isinstance(rates[0], TreasuryRate)
        assert rates[0].date == date(2026, 7, 2)
        assert rates[0].month_1 == 4.32
        assert rates[0].year_10 == 4.12
        assert rates[0].source == "fmp"

    def test_accepts_alt_tenor_keys(self, provider: FMPProvider) -> None:
        payload = [{"date": "2026-07-02", "1M": 4.32, "10Y": 4.12}]
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            rates = provider.get_treasury_rates()

        assert rates[0].month_1 == 4.32
        assert rates[0].year_10 == 4.12

    def test_empty_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            assert provider.get_treasury_rates() == []

    def test_error_dict_raises_provider_error(self, provider: FMPProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({"Error Message": "not in subscription"}),
        ):
            with pytest.raises(ProviderError, match="not in subscription"):
                provider.get_treasury_rates()


# -----------------------------------------------------------------------
# get_forward_estimates
# -----------------------------------------------------------------------


class TestGetForwardEstimates:
    _estimates_data = [
        {
            "date": "2024-12-31",
            "estimatedEpsAvg": 6.80,
            "estimatedRevenueAvg": 400_000_000_000,
        },
        {
            "date": "2025-12-31",
            "estimatedEpsAvg": 7.50,
            "estimatedRevenueAvg": 430_000_000_000,
        },
    ]

    def test_returns_estimates(self, provider: FMPProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._estimates_data)
        ):
            results = provider.get_forward_estimates("AAPL")
        assert len(results) == 2
        assert all(isinstance(r, ForwardEstimates) for r in results)
        assert results[0].eps_estimate == 6.80
        assert results[0].source == "fmp"

    def test_empty_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            results = provider.get_forward_estimates("AAPL")
        assert results == []

    def test_item_with_invalid_date(self, provider: FMPProvider) -> None:
        data = [{"date": "not-a-date", "estimatedEpsAvg": 6.80}]
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_forward_estimates("AAPL")
        assert len(results) == 1
        assert results[0].period == "forward"


# -----------------------------------------------------------------------
# Edge cases — covering remaining missing lines
# -----------------------------------------------------------------------


class TestFmpEdgeCases:
    def test_rate_limit_signals_limit_reach_in_body(self, provider: FMPProvider) -> None:
        resp = _mock_response("Limit Reach for free tier", status_code=403)
        is_limit, _ = provider._rate_limit_signals(resp)
        assert is_limit is True

    def test_rate_limit_signals_json_parse_error(self, provider: FMPProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.side_effect = ValueError("bad json")
        is_limit, _ = provider._rate_limit_signals(resp)
        assert is_limit is False

    def test_intraday_price_history(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "date": "2024-01-02 09:30:00",
                "open": 184.22,
                "high": 185.00,
                "low": 184.00,
                "close": 184.75,
                "volume": 1_500_000,
            }
        ]
        with patch.object(provider._client, "get", return_value=_mock_response(mock_data)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 2), "5m")
        assert len(bars) == 1
        assert bars[0].timestamp is not None

    def test_bad_price_bar_skipped(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "date": "2024-01-02",
                "open": "bad",
                "high": 185.0,
                "low": 184.0,
                "close": 184.75,
                "volume": 1000,
            },
        ]
        with patch.object(provider._client, "get", return_value=_mock_response(mock_data)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 2))
        assert bars == []

    def test_get_financials_empty_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            results = provider.get_financials("AAPL", statement="income", period="annual")
        assert results == []

    def test_get_ratios_empty_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            results = provider.get_ratios("AAPL", period="annual")
        assert results == []

    def test_get_earnings_empty_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            results = provider.get_earnings("AAPL")
        assert results == []

    def test_get_earnings_skips_item_without_date(self, provider: FMPProvider) -> None:
        data = [{"actual": 2.18, "estimate": 2.10}]  # no "date" key
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_earnings("AAPL")
        assert results == []

    def test_insider_trades_missing_filing_date_skipped(self, provider: FMPProvider) -> None:
        data = [{"reportingName": "Tim Cook", "securitiesTransacted": 50000}]
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_insider_trades("AAPL")
        assert results == []

    def test_insider_trades_exercise_type(self, provider: FMPProvider) -> None:
        data = [
            {
                "filingDate": "2024-01-15",
                "transactionDate": "2024-01-12",
                "reportingName": "Tim Cook",
                "transactionType": "M-Exercise",
                "securitiesTransacted": 10000,
                "price": 150.0,
                "value": 1500000,
                "securitiesOwned": 400000,
            }
        ]
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_insider_trades("AAPL")
        assert len(results) == 1
        assert results[0].trade_type == "exercise"

    def test_insider_trades_unknown_type(self, provider: FMPProvider) -> None:
        data = [
            {
                "filingDate": "2024-01-15",
                "transactionDate": "2024-01-12",
                "reportingName": "Tim Cook",
                "transactionType": "G-Gift",
                "securitiesTransacted": 5000,
            }
        ]
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_insider_trades("AAPL")
        assert len(results) == 1
        assert results[0].trade_type == "g-gift"


# -----------------------------------------------------------------------
# get_earnings_calendar
# -----------------------------------------------------------------------


class TestGetEarningsCalendar:
    _calendar_data = [
        {
            "symbol": "AAPL",
            "date": "2025-07-24",
            "epsActual": None,
            "epsEstimated": 1.40,
            "revenueActual": None,
            "revenueEstimated": 94500000000.0,
            "lastUpdated": "2025-06-01",
        },
        {
            "symbol": "MSFT",
            "date": "2025-07-23",
            "epsActual": None,
            "epsEstimated": 3.10,
            "revenueActual": None,
            "revenueEstimated": 70000000000.0,
            "lastUpdated": "2025-06-01",
        },
    ]

    def test_returns_entries(self, provider: FMPProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._calendar_data)
        ):
            results = provider.get_earnings_calendar()
        assert len(results) == 2
        assert all(isinstance(r, EarningsCalendarEntry) for r in results)
        assert results[0].symbol == "AAPL"
        assert results[0].eps_estimate == 1.40
        assert results[0].source == "fmp"
        assert results[0].time_of_day is None

    def test_returns_entries_with_date_range(self, provider: FMPProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(self._calendar_data)
        ):
            results = provider.get_earnings_calendar(start=date(2025, 7, 21), end=date(2025, 7, 25))
        assert len(results) == 2

    def test_empty_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            results = provider.get_earnings_calendar()
        assert results == []

    def test_non_list_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({})):
            results = provider.get_earnings_calendar()
        assert results == []

    def test_skips_entry_without_symbol(self, provider: FMPProvider) -> None:
        data = [
            {"date": "2025-07-24", "epsEstimated": 1.40},
            {
                "symbol": "AAPL",
                "date": "2025-07-24",
                "epsEstimated": 1.40,
            },
        ]
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_earnings_calendar()
        assert len(results) == 1
        assert results[0].symbol == "AAPL"

    def test_skips_entry_without_date(self, provider: FMPProvider) -> None:
        data = [
            {"symbol": "AAPL", "epsEstimated": 1.40},
        ]
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_earnings_calendar()
        assert results == []

    def test_supports_earnings_calendar_endpoint(self, provider: FMPProvider) -> None:
        assert provider.supports("earnings_calendar") is True


class TestGetShortInterest:
    def test_returns_short_interest(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "symbol": "AAPL",
                "date": "2026-05-31",
                "shortFloat": "2.50%",
                "shortRatio": "1.20",
                "sharesShort": 50_000_000,
            }
        ]
        resp = _mock_response(mock_data)
        with patch.object(provider._client, "get", return_value=resp):
            result = provider.get_short_interest("AAPL")

        assert isinstance(result, ShortInterest)
        assert result.symbol == "AAPL"
        assert result.short_interest == 50_000_000
        assert result.short_float_pct is not None
        assert abs(result.short_float_pct - 2.50) < 0.01
        assert result.days_to_cover is not None
        assert abs(result.days_to_cover - 1.20) < 0.01
        assert result.settlement_date == date(2026, 5, 31)

    def test_empty_response_raises(self, provider: FMPProvider) -> None:
        resp = _mock_response([])
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError):
                provider.get_short_interest("AAPL")

    def test_supports_short_interest_endpoint(self, provider: FMPProvider) -> None:
        assert provider.supports("short_interest") is True


# -----------------------------------------------------------------------
# get_economic_calendar
# -----------------------------------------------------------------------


class TestGetEconomicCalendar:
    _event_data = [
        {
            "event": "Initial Jobless Claims",
            "date": "2025-07-03 08:30:00",
            "country": "US",
            "currency": "USD",
            "previous": 234000.0,
            "estimate": 238000.0,
            "actual": None,
            "impact": "High",
            "unit": "K",
        },
        {
            "event": "GDP Growth Rate QoQ",
            "date": "2025-07-30",
            "country": "US",
            "currency": "USD",
            "previous": 1.4,
            "estimate": 2.0,
            "actual": None,
            "impact": "High",
            "unit": "%",
        },
    ]

    def test_returns_events(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._event_data)):
            results = provider.get_economic_calendar()
        assert len(results) == 2
        assert all(isinstance(r, EconomicEvent) for r in results)
        assert results[0].event == "Initial Jobless Claims"
        assert results[0].event_time == "08:30"
        assert results[0].country == "US"
        assert results[0].impact == "high"
        assert results[0].estimate == 238000.0
        assert results[0].source == "fmp"

    def test_date_without_time(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._event_data)):
            results = provider.get_economic_calendar()
        assert results[1].event_time is None
        assert results[1].event_date.isoformat() == "2025-07-30"

    def test_with_date_range(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(self._event_data)):
            results = provider.get_economic_calendar(start=date(2025, 7, 1), end=date(2025, 7, 31))
        assert len(results) == 2

    def test_empty_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            assert provider.get_economic_calendar() == []

    def test_non_list_returns_empty(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({})):
            assert provider.get_economic_calendar() == []

    def test_skips_entry_without_event_name(self, provider: FMPProvider) -> None:
        data = [{"date": "2025-07-03", "country": "US"}]
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            assert provider.get_economic_calendar() == []

    def test_skips_entry_without_date(self, provider: FMPProvider) -> None:
        data = [{"event": "CPI YoY", "country": "US"}]
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            assert provider.get_economic_calendar() == []

    def test_unknown_impact_maps_to_none(self, provider: FMPProvider) -> None:
        data = [{"event": "Some Event", "date": "2025-07-01", "impact": "Unknown"}]
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            results = provider.get_economic_calendar()
        assert results[0].impact is None

    def test_supports_economic_calendar_endpoint(self, provider: FMPProvider) -> None:
        assert provider.supports("economic_calendar") is True


# -----------------------------------------------------------------------
# get_sector_overview
# -----------------------------------------------------------------------

_SECTORS_PAYLOAD = [
    {"sector": "Technology", "changesPercentage": "12.50%", "marketWeight": 0.29},
    {"sector": "Healthcare", "changesPercentage": "3.20%", "marketWeight": 0.13},
    {"sector": "Financial Services", "changesPercentage": "-1.10%", "marketWeight": 0.14},
]


class TestGetSectorOverview:
    def test_returns_sector_info(self, provider: FMPProvider) -> None:
        from onefinance.core.models import SectorInfo

        with patch.object(provider._client, "get", return_value=_mock_response(_SECTORS_PAYLOAD)):
            info = provider.get_sector_overview("Technology")
        assert isinstance(info, SectorInfo)
        assert info.name == "Technology"
        assert info.market_weight == pytest.approx(0.29)
        assert info.source == "fmp"

    def test_ytd_percent_string_parsed(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_SECTORS_PAYLOAD)):
            info = provider.get_sector_overview("Technology")
        # "12.50%" -> 0.1250 (> 1 so divided by 100)
        assert info.ytd_return == pytest.approx(0.125)

    def test_negative_ytd_parsed(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_SECTORS_PAYLOAD)):
            info = provider.get_sector_overview("Financial Services")
        assert info.ytd_return == pytest.approx(-0.011)

    def test_case_insensitive_match(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_SECTORS_PAYLOAD)):
            info = provider.get_sector_overview("technology")
        assert info.name == "Technology"

    def test_unknown_sector_raises(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_SECTORS_PAYLOAD)):
            with pytest.raises(ProviderError, match="not found"):
                provider.get_sector_overview("Unicorn")

    def test_empty_response_raises(self, provider: FMPProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response([])):
            with pytest.raises(ProviderError, match="no data"):
                provider.get_sector_overview("Technology")

    def test_supports_sector_overview(self, provider: FMPProvider) -> None:
        assert provider.supports("sector_overview") is True
