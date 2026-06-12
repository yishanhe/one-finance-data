"""Tests for onefinance.indicators.core."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest

from onefinance.core.models import PriceBar
from onefinance.indicators.core import TechnicalIndicators, compute_indicators

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(
    d: str,
    o: float,
    h: float,
    lo: float,
    c: float,
    v: int = 1_000_000,
) -> PriceBar:
    """Shorthand to build a PriceBar."""
    return PriceBar(
        symbol="TEST",
        date=date.fromisoformat(d),
        open=o,
        high=h,
        low=lo,
        close=c,
        adj_close=c,
        volume=v,
        source="test",
        fetched_at=datetime.now(UTC),
    )


def _make_bars(closes: Sequence[float], base_date: str = "2024-01-01") -> list[PriceBar]:
    """Build bars from a list of close prices (open=high=low=close)."""
    from datetime import timedelta

    start = date.fromisoformat(base_date)
    bars = []
    for i, c in enumerate(closes):
        dt = start + timedelta(days=i)
        bars.append(_bar(dt.isoformat(), c, c * 1.01, c * 0.99, c))
    return bars


# ---------------------------------------------------------------------------
# Basic validation
# ---------------------------------------------------------------------------


class TestBasicValidation:
    """Input validation."""

    def test_too_few_bars_raises(self) -> None:
        bars = _make_bars([100, 101, 102, 103])  # 4 bars
        with pytest.raises(ValueError, match="at least 5"):
            compute_indicators(bars)

    def test_minimum_5_bars_works(self) -> None:
        bars = _make_bars([100, 101, 102, 103, 104])
        result = compute_indicators(bars)
        assert isinstance(result, TechnicalIndicators)
        assert result.ma5 is not None

    def test_output_is_frozen(self) -> None:
        bars = _make_bars([100] * 30)
        result = compute_indicators(bars)
        with pytest.raises(Exception):
            result.ma5 = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Moving Averages
# ---------------------------------------------------------------------------


class TestMovingAverages:
    """MA5, MA10, MA20, MA60 computation."""

    def test_ma5_exact(self) -> None:
        bars = _make_bars([10, 20, 30, 40, 50])
        result = compute_indicators(bars)
        assert result.ma5 == 30.0  # (10+20+30+40+50)/5

    def test_ma10_with_enough_bars(self) -> None:
        bars = _make_bars(list(range(1, 12)))  # 11 bars
        result = compute_indicators(bars)
        assert result.ma10 is not None
        assert result.ma10 == sum(range(2, 12)) / 10  # last 10

    def test_ma20_none_when_insufficient(self) -> None:
        bars = _make_bars([100] * 15)
        result = compute_indicators(bars)
        assert result.ma20 is None

    def test_ma60_none_when_insufficient(self) -> None:
        bars = _make_bars([100] * 30)
        result = compute_indicators(bars)
        assert result.ma60 is None

    def test_ma60_present_with_60_bars(self) -> None:
        bars = _make_bars([100] * 60)
        result = compute_indicators(bars)
        assert result.ma60 == 100.0


# ---------------------------------------------------------------------------
# Bias
# ---------------------------------------------------------------------------


class TestBias:
    """Price deviation from MA."""

    def test_bias_positive(self) -> None:
        # Last 5 closes: 100,100,105,108,115 → MA5 = 105.6 → close 115 is above MA5
        closes = [90, 90, 90, 90, 90, 100, 100, 105, 108, 115]
        bars = _make_bars(closes)
        result = compute_indicators(bars)
        assert result.bias_ma5 is not None
        assert result.bias_ma5 > 0

    def test_bias_status_safe(self) -> None:
        bars = _make_bars([100] * 10)  # flat → bias ≈ 0
        result = compute_indicators(bars)
        assert result.bias_status == "safe"


# ---------------------------------------------------------------------------
# MA Alignment & Trend
# ---------------------------------------------------------------------------


class TestMaAlignment:
    """MA alignment and 5-level trend status."""

    def test_bullish_alignment(self) -> None:
        # Steadily rising prices → MA5 > MA10 > MA20
        closes = list(range(80, 105))  # 25 bars, rising
        bars = _make_bars(closes)
        result = compute_indicators(bars)
        assert result.ma_alignment == "bullish"
        assert result.trend_status in ("BULL", "STRONG_BULL")

    def test_bearish_alignment(self) -> None:
        # Steadily falling prices → MA5 < MA10 < MA20
        closes = list(range(125, 100, -1))  # 25 bars, falling
        bars = _make_bars(closes)
        result = compute_indicators(bars)
        assert result.ma_alignment == "bearish"
        assert result.trend_status in ("BEAR", "STRONG_BEAR")

    def test_mixed_alignment(self) -> None:
        # Choppy with recent downtick so MA5 dips below MA10 but MA10 > MA20
        closes = [
            100,
            105,
            110,
            108,
            112,
            115,
            113,
            118,
            120,
            115,
            110,
            108,
            112,
            115,
            118,
            120,
            122,
            118,
            115,
            110,
            105,
        ]
        bars = _make_bars(closes)
        result = compute_indicators(bars)
        # With a final downtick, MA5 should be below MA10 but MA10 above MA20
        assert result.ma_alignment in ("mixed", "bearish")

    def test_unknown_when_insufficient(self) -> None:
        bars = _make_bars([100] * 8)  # not enough for MA20
        result = compute_indicators(bars)
        assert result.ma_alignment == "unknown"
        assert result.trend_status == "NEUTRAL"


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------


class TestMACD:
    """MACD (12, 26, 9) indicator."""

    def test_macd_none_with_few_bars(self) -> None:
        bars = _make_bars([100] * 20)  # < 26
        result = compute_indicators(bars)
        assert result.macd_dif is None
        assert result.macd_dea is None
        assert result.macd_bar is None

    def test_macd_present_with_enough_bars(self) -> None:
        bars = _make_bars([100] * 30)
        result = compute_indicators(bars)
        assert result.macd_dif is not None
        assert result.macd_dea is not None
        assert result.macd_bar is not None

    def test_macd_flat_is_near_zero(self) -> None:
        bars = _make_bars([100] * 60)
        result = compute_indicators(bars)
        assert result.macd_dif is not None
        assert result.macd_dea is not None
        assert result.macd_bar is not None
        assert abs(result.macd_dif) < 0.01
        assert abs(result.macd_dea) < 0.01
        assert abs(result.macd_bar) < 0.01


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------


class TestRSI:
    """RSI (Wilder, 14)."""

    def test_rsi_none_with_few_bars(self) -> None:
        bars = _make_bars([100] * 10)
        result = compute_indicators(bars)
        assert result.rsi14 is None

    def test_rsi_bullish_above_50(self) -> None:
        # Steadily rising → RSI > 50
        closes = list(range(80, 115))  # 35 bars rising
        bars = _make_bars(closes)
        result = compute_indicators(bars)
        assert result.rsi14 is not None
        assert result.rsi14 > 50

    def test_rsi_bearish_below_50(self) -> None:
        # Steadily falling → RSI < 50
        closes = list(range(115, 80, -1))
        bars = _make_bars(closes)
        result = compute_indicators(bars)
        assert result.rsi14 is not None
        assert result.rsi14 < 50


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


class TestATR:
    """ATR (Wilder, 14)."""

    def test_atr_none_with_few_bars(self) -> None:
        bars = _make_bars([100] * 10)
        result = compute_indicators(bars)
        assert result.atr14 is None

    def test_atr_present_with_enough_bars(self) -> None:
        bars = _make_bars([100] * 20)
        result = compute_indicators(bars)
        assert result.atr14 is not None
        assert result.atr14 >= 0

    def test_atr_pct_is_relative(self) -> None:
        bars = _make_bars([100] * 20)
        result = compute_indicators(bars)
        assert result.atr_pct is not None
        assert result.atr14 is not None
        # ATR% = ATR / close * 100
        expected = round(result.atr14 / 100.0 * 100, 2)
        assert result.atr_pct == expected


# ---------------------------------------------------------------------------
# Volume Ratio
# ---------------------------------------------------------------------------


class TestVolumeRatio:
    """Volume ratio (current / 5-day MA of prior volume)."""

    def test_volume_ratio_uniform(self) -> None:
        # All same volume → ratio ≈ 1.0
        bars = _make_bars([100] * 10)
        result = compute_indicators(bars)
        assert result.volume_ratio == 1.0

    def test_volume_ratio_none_with_few_bars(self) -> None:
        bars = _make_bars([100] * 5)  # need 6 for shifted MA5
        result = compute_indicators(bars)
        assert result.volume_ratio is None


# ---------------------------------------------------------------------------
# Support / Resistance
# ---------------------------------------------------------------------------


class TestSupportResistance:
    """Support and resistance level detection."""

    def test_support_from_ma_below_price(self) -> None:
        # Rising prices: MAs are below current close
        closes = list(range(80, 105))  # 25 bars
        bars = _make_bars(closes)
        result = compute_indicators(bars)
        # At least one MA should be below current price
        assert len(result.support_levels) > 0
        for s in result.support_levels:
            assert s < bars[-1].close

    def test_resistance_from_recent_highs(self) -> None:
        # Create bars where recent highs are above current close
        closes = [100] * 20 + [95]  # drop at end
        bars = _make_bars(closes)
        result = compute_indicators(bars)
        # The recent highs at 100*1.01 are above 95
        for r in result.resistance_levels:
            assert r > bars[-1].close


# ---------------------------------------------------------------------------
# Integration: realistic data
# ---------------------------------------------------------------------------


class TestRealisticData:
    """Test with more realistic price action."""

    def test_full_computation(self) -> None:
        """Verify all fields are populated with 60+ bars."""
        import random

        random.seed(42)
        price = 150.0
        closes = []
        for _ in range(70):
            price *= 1 + random.gauss(0, 0.02)
            closes.append(round(price, 2))

        bars = []
        for i, c in enumerate(closes):
            day = 1 + i
            m = 1 + (day - 1) // 28
            d = ((day - 1) % 28) + 1
            bars.append(
                _bar(
                    f"2024-{m:02d}-{d:02d}",
                    c * 0.99,
                    c * 1.02,
                    c * 0.98,
                    c,
                    v=int(1_000_000 * (1 + random.random())),
                )
            )

        result = compute_indicators(bars)

        # All indicators should be populated
        assert result.ma5 is not None
        assert result.ma10 is not None
        assert result.ma20 is not None
        assert result.ma60 is not None
        assert result.macd_dif is not None
        assert result.rsi14 is not None
        assert result.atr14 is not None
        assert result.volume_ratio is not None
        assert result.ma_alignment in ("bullish", "bearish", "mixed")
        assert result.trend_status in ("STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "STRONG_BEAR")


class TestBollingerBands:
    def test_bb_fields_present_with_enough_bars(self) -> None:
        bars = _make_bars([float(i) for i in range(1, 26)])  # 25 bars, last close=25
        result = compute_indicators(bars)
        assert result.bb_upper is not None
        assert result.bb_lower is not None
        assert result.bb_bandwidth is not None

    def test_bb_ordering(self) -> None:
        bars = _make_bars([float(i) for i in range(1, 26)])
        result = compute_indicators(bars)
        assert result.bb_upper >= result.bb_lower  # type: ignore[operator]

    def test_bb_pct_b_none_when_bandwidth_zero(self) -> None:
        # All same price → std=0 → upper=lower → pct_b undefined
        bars = _make_bars([100.0] * 25)
        result = compute_indicators(bars)
        assert result.bb_bandwidth == 0.0
        assert result.bb_pct_b is None

    def test_bb_pct_b_above_one_when_close_above_upper(self) -> None:
        # Large spike at end pushes close above upper band
        closes = [100.0] * 19 + [200.0]
        bars = _make_bars(closes)
        result = compute_indicators(bars)
        if result.bb_pct_b is not None:
            assert result.bb_pct_b > 1.0

    def test_bb_none_when_fewer_than_20_bars(self) -> None:
        bars = _make_bars([float(i) for i in range(1, 16)])  # only 15 bars
        result = compute_indicators(bars)
        assert result.bb_upper is None
        assert result.bb_lower is None
        assert result.bb_pct_b is None
        assert result.bb_bandwidth is None
