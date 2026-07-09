"""Tests for OneFinanceClient.get_indicators()."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest

from onefinance.core.client import OneFinanceClient
from onefinance.core.errors import AllProvidersFailedError
from onefinance.core.models import PriceBar, Quote
from onefinance.indicators.core import TechnicalIndicators


def _quote(price: float = 200.0, ts: datetime | None = None) -> Quote:
    ts = ts or datetime.now(UTC)
    return Quote(
        symbol="TEST",
        timestamp=ts,
        price=price,
        volume=1_000,
        source="fake",
        fetched_at=ts,
    )


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
        with (
            patch.object(OneFinanceClient, "get_price_history", return_value=_bars(70)),
            patch.object(OneFinanceClient, "get_quote", return_value=_quote()),
        ):
            result = client.get_indicators("AAPL")
        assert isinstance(result, TechnicalIndicators)
        assert result.ma5 is not None
        assert result.ma60 is not None
        assert result.rsi14 is not None
        assert result.trend_status in ("STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "STRONG_BEAR")

    def test_default_lookback_is_180_days(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with (
            patch.object(OneFinanceClient, "get_price_history", return_value=_bars(70)) as mock_get,
            patch.object(OneFinanceClient, "get_quote", return_value=_quote()),
        ):
            client.get_indicators("AAPL")
        kwargs = mock_get.call_args.kwargs
        delta = kwargs["end"] - kwargs["start"]
        assert delta == timedelta(days=180)

    def test_explicit_dates_passed_through(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with (
            patch.object(OneFinanceClient, "get_price_history", return_value=_bars(70)) as mock_get,
            patch.object(OneFinanceClient, "get_quote", return_value=_quote()),
        ):
            client.get_indicators("AAPL", start="2024-01-01", end="2024-06-30")
        kwargs = mock_get.call_args.kwargs
        assert kwargs["start"] == date(2024, 1, 1)
        assert kwargs["end"] == date(2024, 6, 30)

    def test_passes_no_cache_and_provider(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with (
            patch.object(OneFinanceClient, "get_price_history", return_value=_bars(70)) as mock_get,
            patch.object(OneFinanceClient, "get_quote", return_value=_quote()),
        ):
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

    def test_few_bars_returns_partial_result(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with (
            patch.object(OneFinanceClient, "get_price_history", return_value=_bars(3)),
            patch.object(OneFinanceClient, "get_quote", return_value=_quote()),
        ):
            result = client.get_indicators("AAPL")
        assert result.insufficient_history is True

    def test_zero_bars_raises(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with patch.object(OneFinanceClient, "get_price_history", return_value=[]):
            with pytest.raises(ValueError, match="at least 1"):
                client.get_indicators("AAPL")


class TestGetIndicatorsQuoteReference:
    def test_quote_becomes_reference_price(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        bars = _bars(70)
        quote_ts = datetime.combine(bars[-1].date, datetime.min.time(), tzinfo=UTC)
        with (
            patch.object(OneFinanceClient, "get_price_history", return_value=bars),
            patch.object(
                OneFinanceClient, "get_quote", return_value=_quote(price=150.0, ts=quote_ts)
            ),
        ):
            result = client.get_indicators("AAPL")
        assert result.reference_price == 150.0
        assert result.support_levels_current is not None
        assert result.resistance_levels_current is not None
        assert result.indicator_stale is False

    def test_stale_quote_date_sets_flag(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        bars = _bars(70)  # ends 2024-03-10
        quote_ts = datetime(2024, 3, 15, 17, 0, tzinfo=UTC)  # later trading day
        with (
            patch.object(OneFinanceClient, "get_price_history", return_value=bars),
            patch.object(
                OneFinanceClient, "get_quote", return_value=_quote(price=150.0, ts=quote_ts)
            ),
        ):
            result = client.get_indicators("AAPL")
        assert result.indicator_stale is True
        assert result.stale_reason is not None

    def test_quote_failure_degrades_gracefully(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with (
            patch.object(OneFinanceClient, "get_price_history", return_value=_bars(70)),
            patch.object(
                OneFinanceClient,
                "get_quote",
                side_effect=AllProvidersFailedError(endpoint="quote", failures=[]),
            ),
        ):
            result = client.get_indicators("AAPL")
        assert isinstance(result, TechnicalIndicators)
        assert result.reference_price is None
        assert result.indicator_stale is None

    def test_with_quote_false_skips_quote_fetch(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        with (
            patch.object(OneFinanceClient, "get_price_history", return_value=_bars(70)),
            patch.object(OneFinanceClient, "get_quote", return_value=_quote()) as mock_quote,
        ):
            result = client.get_indicators("AAPL", with_quote=False)
        mock_quote.assert_not_called()
        assert result.reference_price is None
