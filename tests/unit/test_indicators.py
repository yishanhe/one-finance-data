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

    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            compute_indicators([])

    def test_fewer_than_5_bars_returns_partial_result(self) -> None:
        bars = _make_bars([100, 101, 102, 103])  # 4 bars — new listing, thin history
        result = compute_indicators(bars)
        assert isinstance(result, TechnicalIndicators)
        assert result.insufficient_history is True
        assert result.ma5 is None  # not enough closes for a 5-period average
        # Still computes what it legitimately can from the available bars.
        assert result.support_levels == [] or all(
            isinstance(v, float) for v in result.support_levels
        )

    def test_single_bar_does_not_crash(self) -> None:
        bars = _make_bars([100])
        result = compute_indicators(bars)
        assert result.insufficient_history is True
        assert result.ma5 is None
        assert result.rsi14 is None

    def test_minimum_5_bars_works(self) -> None:
        bars = _make_bars([100, 101, 102, 103, 104])
        result = compute_indicators(bars)
        assert isinstance(result, TechnicalIndicators)
        assert result.ma5 is not None
        assert result.insufficient_history is False

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


def _zigzag_closes(n: int = 30, start: float = 100.0) -> list[float]:
    """Rising series with pullbacks (+2, -1, ...) so avg_loss > 0.

    A monotonic ramp pins RSI at 100.0 on every bar (avg_loss == 0), which
    makes the bar-to-bar change identically zero — useless for direction.
    """
    closes = [start]
    for i in range(n - 1):
        closes.append(closes[-1] + (2 if i % 2 == 0 else -1))
    return closes


class TestRSIDirection:
    """RSI direction (bar-over-bar change of RSI(14))."""

    def test_unknown_with_exactly_15_closes(self) -> None:
        # 15 closes = 14 deltas = one RSI value, no prior to compare against.
        result = compute_indicators(_make_bars(_zigzag_closes(15)))
        assert result.rsi14 is not None
        assert result.rsi14_prev is None
        assert result.rsi14_change is None
        assert result.rsi_direction == "unknown"

    def test_known_with_16_closes(self) -> None:
        result = compute_indicators(_make_bars(_zigzag_closes(16)))
        assert result.rsi14_prev is not None
        assert result.rsi_direction != "unknown"

    def test_unknown_with_too_few_bars(self) -> None:
        result = compute_indicators(_make_bars([100] * 10))
        assert result.rsi14 is None
        assert result.rsi14_prev is None
        assert result.rsi_direction == "unknown"

    def test_rising_on_sharp_up_move(self) -> None:
        closes = _zigzag_closes()
        closes[-1] = closes[-2] + 6
        result = compute_indicators(_make_bars(closes))
        assert result.rsi_direction == "rising"
        assert result.rsi14_change is not None and result.rsi14_change > 1.0

    def test_falling_on_sharp_down_move(self) -> None:
        closes = _zigzag_closes()
        closes[-1] = closes[-2] - 8
        result = compute_indicators(_make_bars(closes))
        assert result.rsi_direction == "falling"
        assert result.rsi14_change is not None and result.rsi14_change < -1.0

    def test_flat_inside_band(self) -> None:
        closes = _zigzag_closes()
        closes[-1] = closes[-2] + 0.5
        result = compute_indicators(_make_bars(closes))
        assert result.rsi_direction == "flat"
        assert result.rsi14_change is not None and abs(result.rsi14_change) <= 1.0

    def test_change_matches_rsi_delta(self) -> None:
        result = compute_indicators(_make_bars(_zigzag_closes()))
        assert result.rsi14 is not None
        assert result.rsi14_prev is not None
        assert result.rsi14_change == pytest.approx(result.rsi14 - result.rsi14_prev, abs=0.01)

    def test_prev_matches_rsi_of_truncated_series(self) -> None:
        """rsi14_prev equals rsi14 recomputed on the series minus its last bar."""
        closes = _zigzag_closes()
        full = compute_indicators(_make_bars(closes))
        truncated = compute_indicators(_make_bars(closes[:-1]))
        assert full.rsi14_prev == truncated.rsi14


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


# ---------------------------------------------------------------------------
# Freshness metadata + live-quote-aware classification
# ---------------------------------------------------------------------------


class TestFreshnessMetadata:
    def test_as_of_computed_at_last_close_always_set(self) -> None:
        bars = _make_bars([100.0] * 25)
        result = compute_indicators(bars)
        assert result.as_of == bars[-1].date
        assert result.computed_at is not None
        assert result.computed_at.tzinfo is not None
        assert result.last_close == 100.0

    def test_no_reference_leaves_current_fields_none(self) -> None:
        result = compute_indicators(_make_bars([100.0] * 25))
        assert result.reference_price is None
        assert result.support_levels_current is None
        assert result.resistance_levels_current is None
        assert result.indicator_stale is None
        assert result.stale_reason is None


class TestCurrentLevels:
    def test_ma_above_reference_is_resistance_not_support(self) -> None:
        """The FEEDBACK.md case: after a sharp selloff the MA5 sits above the
        live quote — it must be classified as resistance, never support."""
        closes = [110.0] * 20 + [100.0, 95.0, 90.0, 85.0, 80.0]
        bars = _make_bars(closes)
        # Sharp further selloff intraday: live quote well below every MA.
        result = compute_indicators(bars, reference_price=70.0)

        assert result.reference_price == 70.0
        assert result.support_levels_current is not None
        assert result.resistance_levels_current is not None
        assert all(lvl < 70.0 for lvl in result.support_levels_current)
        assert all(lvl > 70.0 for lvl in result.resistance_levels_current)
        # MA5 (=90) — in the close-based list it counts as "support" (< last
        # close 80 is false here: 90 > 80, so it's excluded there too), but in
        # the current view it must appear on the resistance side.
        assert result.ma5 is not None
        assert result.ma5 > 70.0
        assert result.ma5 in result.resistance_levels_current

    def test_reference_between_levels_splits_candidates(self) -> None:
        closes = [100.0] * 30
        bars = _make_bars(closes)
        result = compute_indicators(bars, reference_price=100.5)
        # MAs (=100) below reference → support; recent highs (=101) above → resistance.
        assert result.support_levels_current is not None
        assert 100.0 in result.support_levels_current
        assert result.resistance_levels_current is not None
        assert all(lvl > 100.5 for lvl in result.resistance_levels_current)


class TestStaleness:
    def test_quote_on_later_trading_day_flags_stale(self) -> None:
        bars = _make_bars([100.0] * 25)  # ends 2024-01-25 (Thursday)
        quote_ts = datetime(2024, 1, 26, 17, 30, tzinfo=UTC)  # Friday 12:30 ET
        result = compute_indicators(bars, reference_price=90.0, reference_time=quote_ts)
        assert result.indicator_stale is True
        assert result.stale_reason is not None
        assert "2024-01-25" in result.stale_reason
        assert "2024-01-26" in result.stale_reason

    def test_quote_same_day_not_stale(self) -> None:
        bars = _make_bars([100.0] * 25)  # ends 2024-01-25
        quote_ts = datetime(2024, 1, 25, 21, 0, tzinfo=UTC)  # 16:00 ET same day
        result = compute_indicators(bars, reference_price=100.0, reference_time=quote_ts)
        assert result.indicator_stale is False
        assert result.stale_reason is None

    def test_weekend_quote_after_friday_bar_not_stale(self) -> None:
        # 2024-01-26 is a Friday; a Saturday-evening quote adds no session.
        bars = _make_bars([100.0] * 26)  # ends 2024-01-26 (Friday)
        quote_ts = datetime(2024, 1, 27, 20, 0, tzinfo=UTC)  # Saturday
        result = compute_indicators(bars, reference_price=100.0, reference_time=quote_ts)
        assert result.indicator_stale is False

    def test_late_utc_evening_same_trading_day_not_stale(self) -> None:
        # 01:00 UTC Jan 26 is 20:00 ET Jan 25 — still the as_of trading date.
        bars = _make_bars([100.0] * 25)  # ends 2024-01-25
        quote_ts = datetime(2024, 1, 26, 1, 0, tzinfo=UTC)
        result = compute_indicators(bars, reference_price=100.0, reference_time=quote_ts)
        assert result.indicator_stale is False

    def test_no_reference_time_means_unknown_staleness(self) -> None:
        bars = _make_bars([100.0] * 25)
        result = compute_indicators(bars, reference_price=100.0)
        assert result.indicator_stale is False  # reference given, no timestamp → assume fresh
        assert result.stale_reason is None
