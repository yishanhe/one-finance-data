"""Unit tests for SecEdgarProvider with mocked SEC JSON responses."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch

import pytest

from onefinance.core.errors import NotSupportedError, ProviderError
from onefinance.core.models import BalanceSheet, CashFlow, IncomeStatement
from onefinance.providers.edgar import SecEdgarProvider

_TICKERS = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}


def _usd(facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"units": {"USD": facts}}


def _eps(facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"units": {"USD/shares": facts}}


# companyfacts fixture: annual (10-K) + quarterly (10-Q), with a year-to-date
# Q3 fact that MUST be filtered out in favour of the discrete ~90-day one.
_FACTS = {
    "facts": {
        "us-gaap": {
            "Revenues": _usd(
                [
                    # annual
                    {
                        "start": "2023-10-01",
                        "end": "2024-09-28",
                        "val": 391e9,
                        "form": "10-K",
                        "fp": "FY",
                        "fy": 2024,
                        "filed": "2024-11-01",
                    },
                    {
                        "start": "2022-10-02",
                        "end": "2023-09-30",
                        "val": 383e9,
                        "form": "10-K",
                        "fp": "FY",
                        "fy": 2023,
                        "filed": "2023-11-03",
                    },
                    # quarterly discrete (~90d) — should be kept
                    {
                        "start": "2024-03-31",
                        "end": "2024-06-29",
                        "val": 85e9,
                        "form": "10-Q",
                        "fp": "Q3",
                        "fy": 2024,
                        "filed": "2024-08-02",
                    },
                    # quarterly YTD (~270d), same end as discrete — should be dropped
                    {
                        "start": "2023-10-01",
                        "end": "2024-06-29",
                        "val": 296e9,
                        "form": "10-Q",
                        "fp": "Q3",
                        "fy": 2024,
                        "filed": "2024-08-02",
                    },
                ]
            ),
            "NetIncomeLoss": _usd(
                [
                    {
                        "start": "2023-10-01",
                        "end": "2024-09-28",
                        "val": 93e9,
                        "form": "10-K",
                        "fp": "FY",
                        "fy": 2024,
                        "filed": "2024-11-01",
                    },
                    {
                        "start": "2024-03-31",
                        "end": "2024-06-29",
                        "val": 21e9,
                        "form": "10-Q",
                        "fp": "Q3",
                        "fy": 2024,
                        "filed": "2024-08-02",
                    },
                    # net income for a period with NO revenue anchor → must not emit a statement
                    {
                        "start": "2022-01-01",
                        "end": "2022-12-31",
                        "val": 99e9,
                        "form": "10-K",
                        "fp": "FY",
                        "fy": 2022,
                        "filed": "2023-01-01",
                    },
                ]
            ),
            "EarningsPerShareDiluted": _eps(
                [
                    {
                        "start": "2023-10-01",
                        "end": "2024-09-28",
                        "val": 6.08,
                        "form": "10-K",
                        "fp": "FY",
                        "fy": 2024,
                        "filed": "2024-11-01",
                    },
                ]
            ),
            "Assets": _usd(
                [
                    {
                        "end": "2024-09-28",
                        "val": 365e9,
                        "form": "10-K",
                        "fp": "FY",
                        "fy": 2024,
                        "filed": "2024-11-01",
                    },
                ]
            ),
            "Liabilities": _usd(
                [
                    {
                        "end": "2024-09-28",
                        "val": 308e9,
                        "form": "10-K",
                        "fp": "FY",
                        "fy": 2024,
                        "filed": "2024-11-01",
                    },
                ]
            ),
            "NetCashProvidedByUsedInOperatingActivities": _usd(
                [
                    {
                        "start": "2023-10-01",
                        "end": "2024-09-28",
                        "val": 118e9,
                        "form": "10-K",
                        "fp": "FY",
                        "fy": 2024,
                        "filed": "2024-11-01",
                    },
                ]
            ),
            "PaymentsToAcquirePropertyPlantAndEquipment": _usd(
                [
                    {
                        "start": "2023-10-01",
                        "end": "2024-09-28",
                        "val": 9e9,
                        "form": "10-K",
                        "fp": "FY",
                        "fy": 2024,
                        "filed": "2024-11-01",
                    },
                ]
            ),
        }
    }
}


@pytest.fixture
def provider() -> SecEdgarProvider:
    return SecEdgarProvider()


def _fake_get(url: str) -> Any:
    if "company_tickers" in url:
        return _TICKERS
    return _FACTS


class TestConstructor:
    def test_name(self) -> None:
        assert SecEdgarProvider().name == "edgar"

    def test_no_key_required(self) -> None:
        # Constructing must not raise even with no env vars.
        with patch.dict("os.environ", {}, clear=True):
            assert SecEdgarProvider().name == "edgar"

    def test_user_agent_from_env(self) -> None:
        with patch.dict("os.environ", {"EDGAR_USER_AGENT": "myapp me@x.com"}):
            assert SecEdgarProvider()._user_agent == "myapp me@x.com"


class TestSupports:
    def test_financials_supported(self) -> None:
        assert SecEdgarProvider().supports("financials")

    def test_others_unsupported(self) -> None:
        p = SecEdgarProvider()
        assert not p.supports("quote")
        assert not p.supports("price_history")
        assert not p.supports("insider_trades")


class TestUnservable:
    """EDGAR can only serve US 10-K/10-Q filers — everything else must signal
    NotSupportedError (router falls through + negative-caches, no cooldown),
    never ProviderError (cooldown) or [] (caches an empty success)."""

    def test_unknown_ticker_not_supported(self, provider: SecEdgarProvider) -> None:
        # No CIK (ETF / non-filer / unknown) → NotSupportedError, not cooldown.
        with patch.object(provider, "_get_json", side_effect=_fake_get):
            with pytest.raises(NotSupportedError):
                provider.get_financials("ZZZZ", "income", "annual")

    def test_foreign_filer_not_supported(self, provider: SecEdgarProvider) -> None:
        # Valid CIK + companyfacts, but only 20-F facts (no 10-K/10-Q) → the
        # parse yields zero periods. Must raise NotSupportedError, not return [].
        adr_facts = {
            "facts": {
                "us-gaap": {
                    "Revenues": _usd(
                        [
                            {
                                "start": "2023-01-01",
                                "end": "2023-12-31",
                                "val": 70e9,
                                "form": "20-F",
                                "fp": "FY",
                                "fy": 2023,
                                "filed": "2024-04-01",
                            },
                        ]
                    )
                }
            }
        }

        def fake(url: str) -> Any:
            return _TICKERS if "company_tickers" in url else adr_facts

        with patch.object(provider, "_get_json", side_effect=fake):
            with pytest.raises(NotSupportedError):
                provider.get_financials("AAPL", "income", "annual")


class TestIncomeStatement:
    def test_annual(self, provider: SecEdgarProvider) -> None:
        with patch.object(provider, "_get_json", side_effect=_fake_get):
            rows = provider.get_financials("AAPL", "income", "annual")
        assert [r.period for r in rows] == ["2024-FY", "2023-FY"]
        latest = rows[0]
        assert isinstance(latest, IncomeStatement)
        assert latest.revenue == 391e9
        assert latest.net_income == 93e9
        assert latest.eps_diluted == 6.08
        assert latest.fiscal_date == date(2024, 9, 28)

    def test_quarterly_excludes_ytd(self, provider: SecEdgarProvider) -> None:
        with patch.object(provider, "_get_json", side_effect=_fake_get):
            rows = provider.get_financials("AAPL", "income", "quarterly")
        # Exactly one quarter, and it's the discrete (85e9) value — never the
        # year-to-date 296e9 fact that shares the same end date.
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, IncomeStatement)
        assert row.period == "2024-Q3"
        assert row.revenue == 85e9
        assert row.net_income == 21e9

    def test_anchor_skip(self, provider: SecEdgarProvider) -> None:
        # The 2022 net-income fact has no matching revenue anchor → no statement.
        with patch.object(provider, "_get_json", side_effect=_fake_get):
            rows = provider.get_financials("AAPL", "income", "annual")
        assert all(r.fiscal_date.year != 2022 for r in rows)


class TestBalanceSheet:
    def test_annual(self, provider: SecEdgarProvider) -> None:
        with patch.object(provider, "_get_json", side_effect=_fake_get):
            rows = provider.get_financials("AAPL", "balance", "annual")
        assert len(rows) == 1
        assert isinstance(rows[0], BalanceSheet)
        assert rows[0].total_assets == 365e9
        assert rows[0].total_liabilities == 308e9


class TestCashFlow:
    def test_annual_derives_fcf(self, provider: SecEdgarProvider) -> None:
        with patch.object(provider, "_get_json", side_effect=_fake_get):
            rows = provider.get_financials("AAPL", "cashflow", "annual")
        assert len(rows) == 1
        assert isinstance(rows[0], CashFlow)
        assert rows[0].operating_cash_flow == 118e9
        assert rows[0].capital_expenditure == 9e9
        assert rows[0].free_cash_flow == 118e9 - 9e9


class TestBadStatement:
    def test_invalid_statement_raises(self, provider: SecEdgarProvider) -> None:
        with pytest.raises(ProviderError) as exc:
            provider.get_financials("AAPL", "bogus", "annual")
        assert exc.value.code == "INVALID_ARGUMENT"
