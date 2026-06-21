"""Integration tests that hit the real SEC EDGAR API.

Gated by the ``integration`` marker — run with:

    pytest -m integration

No API key required; SEC requests a descriptive User-Agent (EDGAR_USER_AGENT).
"""

from __future__ import annotations

import pytest

from onefinance.core.models import BalanceSheet, CashFlow, IncomeStatement
from onefinance.providers.edgar import SecEdgarProvider

pytestmark = pytest.mark.integration


@pytest.fixture
def edgar() -> SecEdgarProvider:
    return SecEdgarProvider()


def test_income_annual_apple(edgar: SecEdgarProvider) -> None:
    rows = edgar.get_financials("AAPL", "income", "annual")
    assert rows, "expected annual income statements for AAPL"
    latest = rows[0]
    assert isinstance(latest, IncomeStatement)
    # Apple's revenue is comfortably > $100B; sanity-check the reconstruction.
    assert latest.revenue > 100e9
    assert latest.net_income != 0.0
    assert latest.period.endswith("-FY")


def test_income_quarterly_is_discrete(edgar: SecEdgarProvider) -> None:
    rows = edgar.get_financials("AAPL", "income", "quarterly")
    assert rows, "expected quarterly income statements for AAPL"
    for r in rows:
        assert isinstance(r, IncomeStatement)
        # A single discrete quarter must be well under the annual revenue —
        # proves the year-to-date cumulative fact was filtered out, not summed.
        assert 0 < r.revenue < 200e9
        assert not r.period.endswith("Q4")  # Q4 omitted by design


def test_balance_and_cashflow(edgar: SecEdgarProvider) -> None:
    bal = edgar.get_financials("AAPL", "balance", "annual")
    assert bal
    first_bal = bal[0]
    assert isinstance(first_bal, BalanceSheet)
    assert first_bal.total_assets > 100e9

    cf = edgar.get_financials("AAPL", "cashflow", "annual")
    assert cf
    first_cf = cf[0]
    assert isinstance(first_cf, CashFlow)
    assert first_cf.operating_cash_flow > 0
