"""Integration tests that hit real APIs.

Gated by the ``integration`` marker — run with:

    pytest -m integration

Requires ``FMP_API_KEY`` in environment.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from onefinance.core.models import (
    CompanyInfo,
    FinancialRatios,
    IncomeStatement,
    PriceBar,
    Quote,
    ShortInterest,
)
from onefinance.providers.fmp import FMPProvider

pytestmark = pytest.mark.integration


@pytest.fixture
def fmp_provider() -> FMPProvider:
    """Create an FMPProvider if API key is available, otherwise skip."""
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        pytest.skip("FMP_API_KEY not set")

    return FMPProvider(api_key=api_key)


class TestFMPIntegration:
    def test_get_quote(self, fmp_provider: FMPProvider) -> None:
        quote = fmp_provider.get_quote("AAPL")
        assert isinstance(quote, Quote)
        assert quote.symbol == "AAPL"
        assert quote.price > 0

    def test_get_price_history(self, fmp_provider: FMPProvider) -> None:
        bars = fmp_provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 10))
        assert len(bars) > 0
        assert all(isinstance(b, PriceBar) for b in bars)
        # Should be chronological
        dates = [b.date for b in bars]
        assert dates == sorted(dates)

    def test_get_info(self, fmp_provider: FMPProvider) -> None:
        info = fmp_provider.get_info("AAPL")
        assert isinstance(info, CompanyInfo)
        assert info.name == "Apple Inc."
        assert info.sector is not None

    def test_get_financials_income(self, fmp_provider: FMPProvider) -> None:
        stmts = fmp_provider.get_financials("AAPL", "income", "annual")
        assert len(stmts) > 0
        assert isinstance(stmts[0], IncomeStatement)
        assert stmts[0].revenue > 0

    def test_get_ratios(self, fmp_provider: FMPProvider) -> None:
        ratios = fmp_provider.get_ratios("AAPL", "annual")
        assert len(ratios) > 0
        assert isinstance(ratios[0], FinancialRatios)

    def test_get_short_interest(self, fmp_provider: FMPProvider) -> None:
        result = fmp_provider.get_short_interest("AAPL")
        assert isinstance(result, ShortInterest)
        assert result.symbol == "AAPL"
