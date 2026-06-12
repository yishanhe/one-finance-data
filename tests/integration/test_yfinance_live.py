"""Integration tests for yfinance-backed endpoints via OneFinanceClient.

Gated by the ``integration`` marker — run with:

    pytest -m integration

No API key required (yfinance is free), but requires network access.
"""

from __future__ import annotations

import pytest

from onefinance import OneFinanceClient, OptionsAnalytics
from onefinance.providers.yfinance_provider import YFinanceProvider

pytestmark = pytest.mark.integration


@pytest.fixture
def yf_client() -> OneFinanceClient:
    return OneFinanceClient(providers=[YFinanceProvider()])


class TestYFinanceOptionsAnalytics:
    def test_get_options_analytics(self, yf_client: OneFinanceClient) -> None:
        result = yf_client.get_options_analytics("AAPL", max_expirations=2)
        assert isinstance(result, OptionsAnalytics)
        assert result.symbol == "AAPL"
        assert result.expirations_used <= 2
        assert result.total_call_volume >= 0
        assert result.total_put_volume >= 0
