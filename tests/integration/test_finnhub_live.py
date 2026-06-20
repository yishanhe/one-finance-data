"""Integration tests for FinnhubProvider hitting the real Finnhub API.

Run with:
    pytest -m integration tests/integration/test_finnhub_live.py

Requires ``FINNHUB_API_KEY`` in environment.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from onefinance.core.errors import NotSupportedError
from onefinance.core.models import (
    CompanyInfo,
    CorporateAction,
    EarningsRecord,
    FinancialRatios,
    InsiderTrade,
    Quote,
)
from onefinance.providers.finnhub import FinnhubProvider

pytestmark = pytest.mark.integration


@pytest.fixture
def finnhub_provider() -> FinnhubProvider:
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        pytest.skip("FINNHUB_API_KEY not set")
    return FinnhubProvider(api_key=api_key)


class TestFinnhubIntegration:
    def test_get_quote(self, finnhub_provider: FinnhubProvider) -> None:
        q = finnhub_provider.get_quote("AAPL")
        assert isinstance(q, Quote)
        assert q.symbol == "AAPL"
        assert q.price > 0
        assert q.source == "finnhub"

    def test_get_price_history(self, finnhub_provider: FinnhubProvider) -> None:
        # Finnhub free plan returns 403 for /stock/candle → NotSupportedError
        with pytest.raises(NotSupportedError):
            finnhub_provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 10))

    def test_get_info(self, finnhub_provider: FinnhubProvider) -> None:
        info = finnhub_provider.get_info("AAPL")
        assert isinstance(info, CompanyInfo)
        assert "Apple" in info.name
        assert info.source == "finnhub"

    def test_get_ratios(self, finnhub_provider: FinnhubProvider) -> None:
        ratios = finnhub_provider.get_ratios("AAPL", "annual")
        assert len(ratios) > 0
        assert isinstance(ratios[0], FinancialRatios)
        assert ratios[0].source == "finnhub"

    def test_get_earnings(self, finnhub_provider: FinnhubProvider) -> None:
        records = finnhub_provider.get_earnings("AAPL")
        assert len(records) > 0
        assert all(isinstance(r, EarningsRecord) for r in records)

    def test_get_insider_trades(self, finnhub_provider: FinnhubProvider) -> None:
        trades = finnhub_provider.get_insider_trades("AAPL")
        assert isinstance(trades, list)
        if trades:
            assert isinstance(trades[0], InsiderTrade)
            assert trades[0].source == "finnhub"

    def test_get_corporate_actions(self, finnhub_provider: FinnhubProvider) -> None:
        actions = finnhub_provider.get_corporate_actions("AAPL")
        assert isinstance(actions, list)
        # AAPL has paid dividends for years; expect at least one
        assert len(actions) > 0
        assert all(isinstance(a, CorporateAction) for a in actions)
        assert all(a.source == "finnhub" for a in actions)
        assert all(a.action_type in ("dividend", "split") for a in actions)
