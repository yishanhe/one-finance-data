"""Integration tests for TwelveDataProvider hitting the real Twelve Data API.

Run with:
    pytest -m integration tests/integration/test_twelve_data_live.py

Requires ``TWELVE_DATA_API_KEY`` in environment.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from onefinance.core.models import PriceBar, Quote
from onefinance.providers.twelve_data import TwelveDataProvider

pytestmark = pytest.mark.integration


@pytest.fixture
def twelve_data_provider() -> TwelveDataProvider:
    api_key = os.environ.get("TWELVE_DATA_API_KEY")
    if not api_key:
        pytest.skip("TWELVE_DATA_API_KEY not set")
    return TwelveDataProvider(api_key=api_key)


class TestTwelveDataIntegration:
    def test_get_quote(self, twelve_data_provider: TwelveDataProvider) -> None:
        q = twelve_data_provider.get_quote("AAPL")
        assert isinstance(q, Quote)
        assert q.symbol == "AAPL"
        assert q.price > 0
        assert q.source == "twelve_data"

    def test_get_price_history(self, twelve_data_provider: TwelveDataProvider) -> None:
        bars = twelve_data_provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 10))
        assert len(bars) > 0
        assert all(isinstance(b, PriceBar) for b in bars)
        dates = [b.date for b in bars]
        assert dates == sorted(dates)

    def test_price_history_symbol_uppercased(
        self, twelve_data_provider: TwelveDataProvider
    ) -> None:
        bars = twelve_data_provider.get_price_history("aapl", date(2024, 1, 2), date(2024, 1, 5))
        assert all(b.symbol == "AAPL" for b in bars)
