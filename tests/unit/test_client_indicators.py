"""Tests for OneFinanceClient.get_indicators()."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest

from onefinance.core.client import OneFinanceClient
from onefinance.core.models import PriceBar
from onefinance.indicators.core import TechnicalIndicators


def _bars(n: int, base_close: float = 100.0) -> list[PriceBar]:
    """Build *n* synthetic bars with monotonically rising close prices."""
    now = datetime.now(UTC)
    bars: list[PriceBar] = []
    for i in range(n):
        c = base_close + i
        bars.append(
            PriceBar(
                symbol="TEST",
                date=date(2024, 1, 1) + timedelta(days=i),
                open=c,
                high=c * 1.01,
                low=c * 0.99,
                close=c,
                adj_close=c,
                volume=1_000_000,
                source="fmp",
                fetched_at=now,
            )
        )
    return bars


class TestGetIndicators:
    def test_returns_technical_indicators(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with patch.object(OneFinanceClient, "get_price_history", return_value=_bars(70)):
            result = client.get_indicators("AAPL")
        assert isinstance(result, TechnicalIndicators)
        assert result.ma5 is not None
        assert result.ma60 is not None
        assert result.rsi14 is not None
        assert result.trend_status in ("STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "STRONG_BEAR")

    def test_default_lookback_is_180_days(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with patch.object(
            OneFinanceClient, "get_price_history", return_value=_bars(70)
        ) as mock_get:
            client.get_indicators("AAPL")
        kwargs = mock_get.call_args.kwargs
        delta = kwargs["end"] - kwargs["start"]
        assert delta == timedelta(days=180)

    def test_explicit_dates_passed_through(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with patch.object(
            OneFinanceClient, "get_price_history", return_value=_bars(70)
        ) as mock_get:
            client.get_indicators("AAPL", start="2024-01-01", end="2024-06-30")
        kwargs = mock_get.call_args.kwargs
        assert kwargs["start"] == date(2024, 1, 1)
        assert kwargs["end"] == date(2024, 6, 30)

    def test_passes_no_cache_and_provider(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with patch.object(
            OneFinanceClient, "get_price_history", return_value=_bars(70)
        ) as mock_get:
            client.get_indicators(
                "AAPL",
                no_cache=True,
                provider="fmp",
                ttl=60,
            )
        kwargs = mock_get.call_args.kwargs
        assert kwargs["no_cache"] is True
        assert kwargs["provider"] == "fmp"
        assert kwargs["ttl"] == 60

    def test_too_few_bars_raises(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with patch.object(OneFinanceClient, "get_price_history", return_value=_bars(3)):
            with pytest.raises(ValueError, match="at least 5"):
                client.get_indicators("AAPL")
