"""Unit tests for AlphaVantageProvider with mocked httpx responses."""

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
    NewsArticle,
    PriceBar,
    Quote,
)
from onefinance.providers.alpha_vantage import AlphaVantageProvider, _parse_av_datetime


@pytest.fixture
def provider() -> AlphaVantageProvider:
    return AlphaVantageProvider(api_key="test_key", timeout=5)


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
        p = AlphaVantageProvider(api_key="my_key")
        assert p._api_key == "my_key"

    def test_api_key_from_env(self) -> None:
        with patch.dict("os.environ", {"ALPHAVANTAGE_API_KEY": "env_key"}):
            p = AlphaVantageProvider()
            assert p._api_key == "env_key"

    def test_missing_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ConfigError):
                AlphaVantageProvider()

    def test_name(self) -> None:
        assert AlphaVantageProvider(api_key="k").name == "alpha_vantage"


# -----------------------------------------------------------------------
# Rate-limit detection
# -----------------------------------------------------------------------


class TestRateLimitDetection:
    def test_http_429_raises(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 429)):
            with pytest.raises(RateLimitError):
                provider._get("GLOBAL_QUOTE")

    def test_note_key_raises(self, provider: AlphaVantageProvider) -> None:
        note = (
            "Thank you for using Alpha Vantage! Our standard API call frequency"
            " is 5 calls per minute and 100 calls per day."
        )
        body = {"Note": note}
        with patch.object(provider._client, "get", return_value=_mock_response(body)):
            with pytest.raises(RateLimitError):
                provider._get("GLOBAL_QUOTE")

    def test_information_key_raises(self, provider: AlphaVantageProvider) -> None:
        info = (
            "Thank you for using Alpha Vantage! Our standard API call frequency"
            " is 25 requests per day. Please subscribe to a premium plan."
        )
        body = {"Information": info}
        with patch.object(provider._client, "get", return_value=_mock_response(body)):
            with pytest.raises(RateLimitError):
                provider._get("GLOBAL_QUOTE")

    def test_is_rate_limited_note(self, provider: AlphaVantageProvider) -> None:
        resp = _mock_response({"Note": "rate limit hit"})
        assert provider.is_rate_limited(resp) is True

    def test_is_rate_limited_information(self, provider: AlphaVantageProvider) -> None:
        resp = _mock_response({"Information": "API call frequency limit"})
        assert provider.is_rate_limited(resp) is True

    def test_cooldown_note_returns_60(self, provider: AlphaVantageProvider) -> None:
        resp = _mock_response({"Note": "rate limit hit"})
        assert provider.cooldown_for(resp) == 60

    def test_cooldown_information_returns_3600(self, provider: AlphaVantageProvider) -> None:
        resp = _mock_response({"Information": "API call frequency limit"})
        assert provider.cooldown_for(resp) == 3600

    def test_no_limit_not_rate_limited(self, provider: AlphaVantageProvider) -> None:
        resp = _mock_response({"Global Quote": {"05. price": "150.00"}})
        assert provider.is_rate_limited(resp) is False


# -----------------------------------------------------------------------
# get_price_history
# -----------------------------------------------------------------------


_PRICE_PAYLOAD = {
    "Meta Data": {
        "1. Information": "Daily Time Series",
        "2. Symbol": "AAPL",
    },
    "Time Series (Daily)": {
        "2024-01-02": {
            "1. open": "185.00",
            "2. high": "186.10",
            "3. low": "183.50",
            "4. close": "184.25",
            "5. adjusted close": "184.25",
            "6. volume": "58414500",
            "7. dividend amount": "0.0000",
            "8. split coefficient": "1.0",
        },
        "2024-01-03": {
            "1. open": "184.00",
            "2. high": "185.50",
            "3. low": "182.00",
            "4. close": "185.64",
            "5. adjusted close": "185.64",
            "6. volume": "52000000",
            "7. dividend amount": "0.0000",
            "8. split coefficient": "1.0",
        },
    },
}


class TestGetPriceHistory:
    def test_returns_bars(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_PRICE_PAYLOAD)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 1, 31))
        assert len(bars) == 2
        assert all(isinstance(b, PriceBar) for b in bars)

    def test_fields_correct(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_PRICE_PAYLOAD)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 1, 31))
        b = bars[0]
        assert b.symbol == "AAPL"
        assert b.close == 184.25
        assert b.adj_close == 184.25
        assert b.volume == 58414500
        assert b.source == "alpha_vantage"

    def test_date_filtering(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_PRICE_PAYLOAD)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 3), date(2024, 1, 3))
        assert len(bars) == 1
        assert bars[0].date == date(2024, 1, 3)

    def test_empty_response_raises(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({})):
            with pytest.raises(ProviderError):
                provider.get_price_history("INVALID", date(2024, 1, 1), date(2024, 1, 31))

    def test_unsupported_interval_raises(self, provider: AlphaVantageProvider) -> None:
        with pytest.raises(ProviderError):
            provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 1, 31), interval="1h")


# -----------------------------------------------------------------------
# get_quote
# -----------------------------------------------------------------------


_QUOTE_PAYLOAD = {
    "Global Quote": {
        "01. symbol": "AAPL",
        "02. open": "185.10",
        "03. high": "186.50",
        "04. low": "184.20",
        "05. price": "185.92",
        "06. volume": "48273600",
        "07. latest trading day": "2024-01-05",
        "08. previous close": "185.64",
        "09. change": "0.28",
        "10. change percent": "0.1509%",
    }
}


class TestGetQuote:
    def test_returns_quote(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_QUOTE_PAYLOAD)):
            q = provider.get_quote("AAPL")
        assert isinstance(q, Quote)
        assert q.symbol == "AAPL"
        assert q.price == 185.92
        assert q.volume == 48273600
        assert q.source == "alpha_vantage"

    def test_empty_global_quote_raises(self, provider: AlphaVantageProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response({"Global Quote": {}})
        ):
            with pytest.raises(ProviderError):
                provider.get_quote("INVALID")


# -----------------------------------------------------------------------
# get_info
# -----------------------------------------------------------------------


_INFO_PAYLOAD = {
    "Symbol": "AAPL",
    "AssetType": "Common Stock",
    "Name": "Apple Inc",
    "Description": "Apple designs and markets consumer electronics.",
    "Exchange": "NASDAQ",
    "Currency": "USD",
    "Country": "USA",
    "Sector": "TECHNOLOGY",
    "Industry": "ELECTRONIC COMPUTERS",
    "OfficialSite": "https://www.apple.com",
    "MarketCapitalization": "2900000000000",
    "Beta": "1.24",
    "SharesOutstanding": "15700000000",
    "FullTimeEmployees": "164000",
}


class TestGetInfo:
    def test_returns_company_info(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_INFO_PAYLOAD)):
            info = provider.get_info("AAPL")
        assert isinstance(info, CompanyInfo)
        assert info.symbol == "AAPL"
        assert info.name == "Apple Inc"
        assert info.exchange == "NASDAQ"
        assert info.sector == "TECHNOLOGY"
        assert info.market_cap == 2_900_000_000_000.0
        assert info.source == "alpha_vantage"

    def test_none_sentinel_fields(self, provider: AlphaVantageProvider) -> None:
        payload = dict(_INFO_PAYLOAD, Beta="None", SharesOutstanding="None")
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            info = provider.get_info("AAPL")
        assert info.beta is None
        assert info.shares_outstanding is None

    def test_missing_symbol_raises(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({})):
            with pytest.raises(ProviderError):
                provider.get_info("INVALID")


# -----------------------------------------------------------------------
# get_financials — income
# -----------------------------------------------------------------------


_INCOME_PAYLOAD = {
    "symbol": "AAPL",
    "annualReports": [
        {
            "fiscalDateEnding": "2023-09-30",
            "reportedCurrency": "USD",
            "totalRevenue": "383285000000",
            "costOfRevenue": "214137000000",
            "grossProfit": "169148000000",
            "operatingIncome": "114301000000",
            "netIncome": "96995000000",
            "eps": "6.16",
            "epsDiluted": "6.13",
            "ebitda": "130000000000",
            "researchAndDevelopment": "29915000000",
            "sellingGeneralAndAdministrative": "24932000000",
        }
    ],
    "quarterlyReports": [],
}


class TestGetFinancialsIncome:
    def test_returns_income_statements(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_INCOME_PAYLOAD)):
            stmts = provider.get_financials("AAPL", "income", "annual")
        assert len(stmts) == 1
        assert isinstance(stmts[0], IncomeStatement)

    def test_fields_correct(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_INCOME_PAYLOAD)):
            stmts = provider.get_financials("AAPL", "income", "annual")
        s = stmts[0]
        assert isinstance(s, IncomeStatement)
        assert s.revenue == 383_285_000_000.0
        assert s.net_income == 96_995_000_000.0
        assert s.ebitda == 130_000_000_000.0
        assert s.source == "alpha_vantage"

    def test_invalid_statement_raises(self, provider: AlphaVantageProvider) -> None:
        with pytest.raises(ProviderError):
            provider.get_financials("AAPL", "unknown", "annual")

    def test_empty_reports_returns_empty_list(self, provider: AlphaVantageProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({"symbol": "AAPL", "annualReports": []}),
        ):
            result = provider.get_financials("AAPL", "income", "annual")
        assert result == []


# -----------------------------------------------------------------------
# get_financials — balance sheet
# -----------------------------------------------------------------------


_BALANCE_PAYLOAD = {
    "symbol": "AAPL",
    "annualReports": [
        {
            "fiscalDateEnding": "2023-09-30",
            "reportedCurrency": "USD",
            "totalAssets": "352583000000",
            "totalLiabilities": "290437000000",
            "totalShareholderEquity": "62146000000",
            "cashAndCashEquivalentsAtCarryingValue": "29965000000",
            "shortLongTermDebtTotal": "111000000000",
            "totalCurrentAssets": "143566000000",
            "totalCurrentLiabilities": "145308000000",
            "goodwill": "None",
            "inventory": "6331000000",
        }
    ],
    "quarterlyReports": [],
}


class TestGetFinancialsBalance:
    def test_returns_balance_sheet(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_BALANCE_PAYLOAD)):
            stmts = provider.get_financials("AAPL", "balance", "annual")
        assert len(stmts) == 1
        assert isinstance(stmts[0], BalanceSheet)

    def test_fields_correct(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_BALANCE_PAYLOAD)):
            stmts = provider.get_financials("AAPL", "balance", "annual")
        b = stmts[0]
        assert isinstance(b, BalanceSheet)
        assert b.total_assets == 352_583_000_000.0
        assert b.goodwill is None
        assert b.inventory == 6_331_000_000.0


# -----------------------------------------------------------------------
# get_financials — cash flow
# -----------------------------------------------------------------------


_CASHFLOW_PAYLOAD = {
    "symbol": "AAPL",
    "annualReports": [
        {
            "fiscalDateEnding": "2023-09-30",
            "reportedCurrency": "USD",
            "operatingCashflow": "110543000000",
            "capitalExpenditures": "-10959000000",
            "dividendPayout": "-15025000000",
            "depreciationDepletionAndAmortization": "11519000000",
            "paymentsForRepurchaseOfCommonStock": "-77550000000",
        }
    ],
    "quarterlyReports": [],
}


class TestGetFinancialsCashFlow:
    def test_returns_cashflow(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_CASHFLOW_PAYLOAD)):
            stmts = provider.get_financials("AAPL", "cashflow", "annual")
        assert len(stmts) == 1
        assert isinstance(stmts[0], CashFlow)

    def test_fields_correct(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_CASHFLOW_PAYLOAD)):
            stmts = provider.get_financials("AAPL", "cashflow", "annual")
        cf = stmts[0]
        assert isinstance(cf, CashFlow)
        assert cf.operating_cash_flow == 110_543_000_000.0
        assert cf.capital_expenditure == 10_959_000_000.0  # absolute value
        assert cf.free_cash_flow == cf.operating_cash_flow - cf.capital_expenditure
        assert cf.dividends_paid == 15_025_000_000.0  # absolute value


# -----------------------------------------------------------------------
# get_earnings
# -----------------------------------------------------------------------


_EARNINGS_PAYLOAD = {
    "symbol": "AAPL",
    "annualEarnings": [
        {"fiscalDateEnding": "2023-09-30", "reportedEPS": "6.16"},
        {"fiscalDateEnding": "2022-09-24", "reportedEPS": "6.15"},
    ],
    "quarterlyEarnings": [
        {
            "fiscalDateEnding": "2023-09-30",
            "reportedDate": "2023-11-02",
            "reportedEPS": "1.46",
            "estimatedEPS": "1.39",
            "surprise": "0.07",
            "surprisePercentage": "5.04",
        }
    ],
}


class TestGetEarnings:
    def test_returns_earnings_records(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_EARNINGS_PAYLOAD)):
            records = provider.get_earnings("AAPL")
        # 1 quarterly + 2 annual
        assert len(records) == 3
        assert all(isinstance(r, EarningsRecord) for r in records)

    def test_quarterly_has_estimate(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_EARNINGS_PAYLOAD)):
            records = provider.get_earnings("AAPL")
        quarterly = [r for r in records if "-Q" in r.period]
        assert len(quarterly) == 1
        assert quarterly[0].eps_actual == 1.46
        assert quarterly[0].eps_estimate == 1.39
        assert quarterly[0].eps_surprise == 0.07

    def test_annual_no_estimate(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_EARNINGS_PAYLOAD)):
            records = provider.get_earnings("AAPL")
        annual = [r for r in records if "-Q" not in r.period]
        assert all(r.eps_estimate is None for r in annual)


# -----------------------------------------------------------------------
# get_news
# -----------------------------------------------------------------------


_NEWS_PAYLOAD = {
    "items": "2",
    "feed": [
        {
            "title": "Apple Reports Record Revenue",
            "url": "https://example.com/apple-revenue",
            "time_published": "20240105T143000",
            "authors": ["Jane Smith"],
            "summary": "Apple Inc. reported record revenue for Q1 2024.",
            "source": "Reuters",
            "overall_sentiment_label": "Bullish",
            "ticker_sentiment": [{"ticker": "AAPL", "relevance_score": "0.9"}],
        },
        {
            "title": "Tech Sector Update",
            "url": "https://example.com/tech-update",
            "time_published": "20240105T120000",
            "authors": [],
            "summary": "Overview of the tech sector.",
            "source": "Bloomberg",
            "overall_sentiment_label": "Neutral",
            "ticker_sentiment": [],
        },
    ],
}


class TestGetNews:
    def test_returns_articles(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_NEWS_PAYLOAD)):
            articles = provider.get_news("AAPL")
        assert len(articles) == 2
        assert all(isinstance(a, NewsArticle) for a in articles)

    def test_fields_correct(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_NEWS_PAYLOAD)):
            articles = provider.get_news("AAPL")
        a = articles[0]
        assert a.title == "Apple Reports Record Revenue"
        assert a.publisher == "Reuters"
        assert a.link == "https://example.com/apple-revenue"
        assert a.source == "alpha_vantage"

    def test_empty_feed_returns_empty(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"feed": []})):
            articles = provider.get_news("AAPL")
        assert articles == []


# -----------------------------------------------------------------------
# supports() capability discovery
# -----------------------------------------------------------------------


class TestSupports:
    def test_supported_endpoints(self) -> None:
        p = AlphaVantageProvider(api_key="k")
        assert p.supports("price_history")
        assert p.supports("quote")
        assert p.supports("info")
        assert p.supports("financials")
        assert p.supports("earnings")
        assert p.supports("news")
        assert p.supports("insider_trades")
        assert p.supports("corporate_actions")
        assert p.supports("earnings_calendar")

    def test_unsupported_endpoints(self) -> None:
        p = AlphaVantageProvider(api_key="k")
        assert not p.supports("dcf")
        assert not p.supports("option_chain")
        assert not p.supports("institutional_holders")


# -----------------------------------------------------------------------
# _parse_av_datetime — ISO fallback branch
# -----------------------------------------------------------------------


class TestParseAvDatetime:
    def test_compact_format(self) -> None:
        dt = _parse_av_datetime("20240105T143000")
        assert dt.year == 2024 and dt.month == 1 and dt.day == 5

    def test_iso_fallback(self) -> None:
        dt = _parse_av_datetime("2024-01-05T14:30:00+00:00")
        assert dt.year == 2024 and dt.month == 1 and dt.day == 5


# -----------------------------------------------------------------------
# _rate_limit_signals — edge cases
# -----------------------------------------------------------------------


class TestRateLimitSignals:
    def test_non_200_non_429_returns_no_limit(self, provider: AlphaVantageProvider) -> None:
        resp = _mock_response({}, 500)
        hit, secs = provider._rate_limit_signals(resp)
        assert not hit
        assert secs is None

    def test_json_parse_error_returns_no_limit(self, provider: AlphaVantageProvider) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.side_effect = Exception("bad json")
        hit, _ = provider._rate_limit_signals(resp)
        assert not hit

    def test_non_dict_json_returns_no_limit(self, provider: AlphaVantageProvider) -> None:
        resp = _mock_response([1, 2, 3])
        hit, _ = provider._rate_limit_signals(resp)
        assert not hit


# -----------------------------------------------------------------------
# is_rate_limited / cooldown_for — non-httpx inputs
# -----------------------------------------------------------------------


class TestRateLimitInterface:
    def test_is_rate_limited_exception_with_limit_text(
        self, provider: AlphaVantageProvider
    ) -> None:
        assert provider.is_rate_limited(Exception("rate limit exceeded")) is True

    def test_is_rate_limited_exception_with_429_text(self, provider: AlphaVantageProvider) -> None:
        assert provider.is_rate_limited(Exception("HTTP 429")) is True

    def test_is_rate_limited_exception_no_match(self, provider: AlphaVantageProvider) -> None:
        assert provider.is_rate_limited(Exception("connection timeout")) is False

    def test_is_rate_limited_non_httpx_returns_false(self, provider: AlphaVantageProvider) -> None:
        assert provider.is_rate_limited("some string") is False

    def test_cooldown_for_non_httpx_returns_default(self, provider: AlphaVantageProvider) -> None:
        result = provider.cooldown_for("not a response")
        assert result == provider._default_rate_limit_cooldown_s


# -----------------------------------------------------------------------
# _get — non-200 and benign Note/Information
# -----------------------------------------------------------------------


class TestGetMethod:
    def test_non_200_raises_provider_error(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 404)):
            with pytest.raises(ProviderError):
                provider._get("GLOBAL_QUOTE")

    def test_benign_note_does_not_raise(self, provider: AlphaVantageProvider) -> None:
        body = {"Note": "Welcome to Alpha Vantage", "Global Quote": {"05. price": "150.00"}}
        with patch.object(provider._client, "get", return_value=_mock_response(body)):
            data = provider._get("GLOBAL_QUOTE")
        assert "Note" in data


# -----------------------------------------------------------------------
# get_financials — quarterly period labels
# -----------------------------------------------------------------------

_INCOME_QUARTERLY_PAYLOAD = {
    "symbol": "AAPL",
    "annualReports": [],
    "quarterlyReports": [
        {
            "fiscalDateEnding": "2023-09-30",
            "reportedCurrency": "USD",
            "totalRevenue": "89498000000",
            "netIncome": "22956000000",
            "ebitda": "29000000000",
            "grossProfit": "40428000000",
            "operatingIncome": "26969000000",
            "researchAndDevelopment": "7307000000",
            "costOfRevenue": "49070000000",
        }
    ],
}


class TestGetFinancialsQuarterly:
    def test_quarterly_income_period_label(self, provider: AlphaVantageProvider) -> None:
        with patch.object(
            provider._client, "get", return_value=_mock_response(_INCOME_QUARTERLY_PAYLOAD)
        ):
            stmts = provider.get_financials("AAPL", "income", "quarterly")
        assert len(stmts) == 1
        s = stmts[0]
        assert isinstance(s, IncomeStatement)
        assert "-Q" in s.period  # e.g. "2023-Q3"


# -----------------------------------------------------------------------
# get_earnings — skip items with missing fiscal date
# -----------------------------------------------------------------------


class TestGetEarningsMissingDate:
    def test_quarterly_skips_missing_date(self, provider: AlphaVantageProvider) -> None:
        payload = {
            "quarterlyEarnings": [
                {"fiscalDateEnding": "", "reportedEPS": "1.46"},
                {
                    "fiscalDateEnding": "2023-09-30",
                    "reportedEPS": "1.46",
                    "estimatedEPS": "1.39",
                    "surprise": "0.07",
                },
            ],
            "annualEarnings": [],
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            records = provider.get_earnings("AAPL")
        assert len(records) == 1

    def test_annual_skips_missing_date(self, provider: AlphaVantageProvider) -> None:
        payload = {
            "quarterlyEarnings": [],
            "annualEarnings": [
                {"fiscalDateEnding": "", "reportedEPS": "6.11"},
                {"fiscalDateEnding": "2023-09-30", "reportedEPS": "6.11"},
            ],
        }
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            records = provider.get_earnings("AAPL")
        assert len(records) == 1


# -----------------------------------------------------------------------
# get_news — edge cases
# -----------------------------------------------------------------------


class TestGetNewsEdgeCases:
    def test_bad_timestamp_falls_back_to_now(self, provider: AlphaVantageProvider) -> None:
        item = {
            "title": "Some News",
            "url": "https://example.com/news",
            "time_published": "NOT_A_DATE",
            "authors": [],
            "summary": "Summary text.",
            "source": "Reuters",
        }
        with patch.object(provider._client, "get", return_value=_mock_response({"feed": [item]})):
            articles = provider.get_news("AAPL")
        assert len(articles) == 1

    def test_skips_article_with_no_title(self, provider: AlphaVantageProvider) -> None:
        items = [
            {
                "title": "",
                "url": "https://example.com/a",
                "time_published": "20240105T143000",
                "source": "X",
            },
            {
                "title": "Valid",
                "url": "https://example.com/b",
                "time_published": "20240105T143000",
                "source": "Y",
            },
        ]
        with patch.object(provider._client, "get", return_value=_mock_response({"feed": items})):
            articles = provider.get_news("AAPL")
        assert len(articles) == 1
        assert articles[0].title == "Valid"

    def test_skips_article_with_no_url(self, provider: AlphaVantageProvider) -> None:
        items = [
            {
                "title": "No URL",
                "url": "",
                "time_published": "20240105T143000",
                "source": "X",
            },
            {
                "title": "Valid",
                "url": "https://example.com/valid",
                "time_published": "20240105T143000",
                "source": "Y",
            },
        ]
        with patch.object(provider._client, "get", return_value=_mock_response({"feed": items})):
            articles = provider.get_news("AAPL")
        assert len(articles) == 1
        assert articles[0].title == "Valid"


_OVERVIEW_PAYLOAD = {
    "Symbol": "AAPL",
    "Name": "Apple Inc",
    "Exchange": "NASDAQ",
    "Sector": "Technology",
    "PERatio": "28.5",
    "PriceToBookRatio": "45.2",
    "PriceToSalesRatioTTM": "7.8",
    "ReturnOnEquityTTM": "1.47",
    "ReturnOnAssetsTTM": "0.22",
    "OperatingMarginTTM": "0.31",
    "ProfitMargin": "0.25",
    "DividendYield": "0.0058",
    "BookValue": "4.20",
    "RevenuePerShareTTM": "25.60",
}


class TestGetRatios:
    def test_returns_financial_ratios(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response(_OVERVIEW_PAYLOAD)):
            ratios = provider.get_ratios("AAPL", "annual")
        assert len(ratios) == 1
        r = ratios[0]
        assert isinstance(r, FinancialRatios)
        assert r.symbol == "AAPL"
        assert r.source == "alpha_vantage"
        assert r.period == "annual"
        assert r.pe_ratio == 28.5
        assert r.pb_ratio == 45.2
        assert r.ps_ratio == 7.8
        assert r.return_on_equity == 1.47
        assert r.return_on_assets == 0.22
        assert r.operating_margin == 0.31
        assert r.net_margin == 0.25
        assert r.dividend_yield == 0.0058
        assert r.book_value_per_share == 4.20
        assert r.revenue_per_share == 25.60
        assert r.fiscal_date == date.today()

    def test_raises_on_empty_response(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({})):
            with pytest.raises(ProviderError):
                provider.get_ratios("AAPL", "annual")

    def test_handles_none_fields(self, provider: AlphaVantageProvider) -> None:
        minimal = {"Symbol": "MSFT", "Name": "Microsoft"}
        with patch.object(provider._client, "get", return_value=_mock_response(minimal)):
            ratios = provider.get_ratios("MSFT", "annual")
        assert len(ratios) == 1
        r = ratios[0]
        assert r.pe_ratio is None
        assert r.pb_ratio is None


# -----------------------------------------------------------------------
# get_insider_trades
# -----------------------------------------------------------------------

_INSIDER_PAYLOAD = {
    "data": [
        {
            "filingDate": "2024-01-15",
            "transactionDate": "2024-01-12",
            "reportingName": "Tim Cook",
            "reportedTitle": "CEO",
            "transactionType": "S-Sale",
            "sharesTraded": "100000",
            "sharePrice": "185.00",
            "sharesOwned": "500000",
        },
        {
            "filingDate": "2024-01-10",
            "transactionDate": "2024-01-08",
            "reportingName": "Luca Maestri",
            "reportedTitle": "CFO",
            "transactionType": "P-Purchase",
            "sharesTraded": "5000",
            "sharePrice": "180.00",
            "sharesOwned": "50000",
        },
    ]
}


class TestGetInsiderTrades:
    def test_returns_trades(self, provider: AlphaVantageProvider) -> None:
        from onefinance.core.models import InsiderTrade

        with patch.object(provider._client, "get", return_value=_mock_response(_INSIDER_PAYLOAD)):
            trades = provider.get_insider_trades("AAPL")
        assert len(trades) == 2
        assert all(isinstance(t, InsiderTrade) for t in trades)
        assert trades[0].trade_type == "sell"
        assert trades[1].trade_type == "buy"
        assert trades[0].insider_name == "Tim Cook"
        assert trades[0].source == "alpha_vantage"

    def test_sell_type_mapped(self, provider: AlphaVantageProvider) -> None:
        payload = {"data": [dict(_INSIDER_PAYLOAD["data"][0], transactionType="S-Sale")]}
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            trades = provider.get_insider_trades("AAPL")
        assert trades[0].trade_type == "sell"

    def test_exercise_type_mapped(self, provider: AlphaVantageProvider) -> None:
        payload = {"data": [dict(_INSIDER_PAYLOAD["data"][0], transactionType="M-Exempt")]}
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            trades = provider.get_insider_trades("AAPL")
        assert trades[0].trade_type == "exercise"

    def test_empty_data_returns_empty(self, provider: AlphaVantageProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"data": []})):
            trades = provider.get_insider_trades("AAPL")
        assert trades == []

    def test_missing_filing_date_skipped(self, provider: AlphaVantageProvider) -> None:
        payload = {"data": [{"reportingName": "X", "transactionType": "P-Purchase"}]}
        with patch.object(provider._client, "get", return_value=_mock_response(payload)):
            trades = provider.get_insider_trades("AAPL")
        assert trades == []


# -----------------------------------------------------------------------
# get_corporate_actions
# -----------------------------------------------------------------------

_DIV_PAYLOAD = {
    "data": [
        {"ex_dividend_date": "2024-02-09", "amount": "0.25"},
        {"ex_dividend_date": "2023-11-10", "amount": "0.24"},
    ]
}
_SPLIT_PAYLOAD = {
    "data": [
        {"effective_date": "2020-08-31", "split_factor": "4/1"},
    ]
}


class TestGetCorporateActions:
    def test_returns_dividends_and_splits(self, provider: AlphaVantageProvider) -> None:
        from onefinance.core.models import CorporateAction

        def _side_effect(url: str, params: dict[str, Any], **kwargs: Any) -> Any:
            if params.get("function") == "DIVIDENDS":
                return _mock_response(_DIV_PAYLOAD)
            return _mock_response(_SPLIT_PAYLOAD)

        with patch.object(provider._client, "get", side_effect=_side_effect):
            actions = provider.get_corporate_actions("AAPL")

        divs = [a for a in actions if a.action_type == "dividend"]
        splits = [a for a in actions if a.action_type == "split"]
        assert len(divs) == 2
        assert len(splits) == 1
        assert splits[0].split_ratio == pytest.approx(4.0)
        assert divs[0].amount == pytest.approx(0.25)
        assert all(isinstance(a, CorporateAction) for a in actions)

    def test_sorted_newest_first(self, provider: AlphaVantageProvider) -> None:
        def _side_effect(url: str, params: dict[str, Any], **kwargs: Any) -> Any:
            if params.get("function") == "DIVIDENDS":
                return _mock_response(_DIV_PAYLOAD)
            return _mock_response(_SPLIT_PAYLOAD)

        with patch.object(provider._client, "get", side_effect=_side_effect):
            actions = provider.get_corporate_actions("AAPL")
        assert actions[0].date >= actions[-1].date

    def test_empty_returns_empty(self, provider: AlphaVantageProvider) -> None:
        with patch.object(
            provider._client,
            "get",
            return_value=_mock_response({"data": []}),
        ):
            actions = provider.get_corporate_actions("AAPL")
        assert actions == []


# -----------------------------------------------------------------------
# get_earnings_calendar
# -----------------------------------------------------------------------

_CALENDAR_CSV = (
    "symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\n"
    "AAPL,Apple Inc,2024-01-30,2023-12-31,2.10,USD\r\n"
    "MSFT,Microsoft Corp,2024-01-24,2023-12-31,2.78,USD\r\n"
)


class TestGetEarningsCalendar:
    def test_returns_entries(self, provider: AlphaVantageProvider) -> None:
        from onefinance.core.models import EarningsCalendarEntry

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _CALENDAR_CSV

        with patch.object(provider, "_request", return_value=mock_resp):
            entries = provider.get_earnings_calendar()
        assert len(entries) == 2
        assert all(isinstance(e, EarningsCalendarEntry) for e in entries)
        assert entries[0].symbol == "AAPL"
        assert entries[0].eps_estimate == pytest.approx(2.10)
        assert entries[0].source == "alpha_vantage"

    def test_date_filter_start(self, provider: AlphaVantageProvider) -> None:
        from datetime import date

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _CALENDAR_CSV

        with patch.object(provider, "_request", return_value=mock_resp):
            entries = provider.get_earnings_calendar(start=date(2024, 1, 25))
        assert len(entries) == 1
        assert entries[0].symbol == "AAPL"

    def test_http_error_raises(self, provider: AlphaVantageProvider) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server Error"

        with patch.object(provider, "_request", return_value=mock_resp):
            with pytest.raises(ProviderError, match="earnings_calendar"):
                provider.get_earnings_calendar()

    def test_empty_csv_returns_empty(self, provider: AlphaVantageProvider) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "symbol,name,reportDate,fiscalDateEnding,estimate,currency\r\n"

        with patch.object(provider, "_request", return_value=mock_resp):
            entries = provider.get_earnings_calendar()
        assert entries == []
