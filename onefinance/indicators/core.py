"""Core technical indicator computations.

All functions operate on plain Python lists — no pandas or numpy
required.  Input is a ``list[PriceBar]`` (sorted oldest-first);
output is a frozen ``TechnicalIndicators`` Pydantic model.

Indicators ported from invest/ ``fetch_stock_data.py`` with
identical math so results are cross-compatible.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from onefinance.cache.manager import _has_trading_days_in_gap
from onefinance.core.models import PriceBar

_EASTERN = ZoneInfo("America/New_York")

# Bar-to-bar RSI moves smaller than this (in RSI points) are reported as
# "flat" — day-to-day noise in RSI(14) is routinely well under a point.
_RSI_FLAT_BAND = 1.0

# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


class TechnicalIndicators(BaseModel):
    """Snapshot of all technical indicators for the most recent bar.

    Freshness contract: every value is computed from the last **completed**
    daily bar (``as_of``, ``last_close``) — intraday price action is never
    included. ``support_levels``/``resistance_levels`` are therefore
    classified relative to ``last_close``, and after a sharp move they can
    invert meaning versus the live quote (an "MA5 support" above spot is
    really overhead resistance). When a live reference price is supplied
    (``OneFinanceClient.get_indicators`` passes the current quote by
    default), the ``*_current`` fields re-classify levels against it and
    ``indicator_stale``/``stale_reason`` flag bars that miss one or more
    completed trading sessions.
    """

    model_config = ConfigDict(frozen=True)

    # Freshness metadata
    as_of: date | None = None  # date of the last bar the values are computed from
    computed_at: datetime | None = None  # UTC wall-clock time of computation
    last_close: float | None = None  # close of the last bar — basis for MAs/bias/levels

    # Live-quote-aware view (None when no reference price was supplied)
    reference_price: float | None = None
    support_levels_current: list[float] | None = None
    resistance_levels_current: list[float] | None = None
    indicator_stale: bool | None = None
    stale_reason: str | None = None

    # Moving averages
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None

    # Bias (deviation of close from MA, in %)
    bias_ma5: float | None = None
    bias_ma10: float | None = None
    bias_ma20: float | None = None
    bias_status: Literal["safe", "caution", "danger", "unknown"] = "unknown"

    # MA alignment & trend
    ma_alignment: Literal["bullish", "bearish", "mixed", "unknown"] = "unknown"
    trend_status: Literal["STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "STRONG_BEAR"] = "NEUTRAL"

    # Volume
    volume_ratio: float | None = None

    # MACD (12, 26, 9)
    macd_dif: float | None = None
    macd_dea: float | None = None
    macd_bar: float | None = None

    # RSI
    rsi14: float | None = None
    rsi14_prev: float | None = None  # RSI of the previous completed bar
    rsi14_change: float | None = None  # rsi14 - rsi14_prev, in RSI points
    # "flat" when |rsi14_change| <= 1.0 RSI point; "unknown" when the prior
    # bar's RSI cannot be computed (needs period + 2 closes).
    rsi_direction: Literal["rising", "falling", "flat", "unknown"] = "unknown"

    # ATR (Wilder, 14)
    atr14: float | None = None
    atr_pct: float | None = None

    # Support / Resistance
    support_levels: list[float] = []
    resistance_levels: list[float] = []

    # Bollinger Bands (20, 2)
    bb_upper: float | None = None
    bb_lower: float | None = None
    bb_pct_b: float | None = None  # (close - lower) / (upper - lower)
    bb_bandwidth: float | None = None  # (upper - lower) / middle * 100

    # True when fewer than 5 bars were supplied — most fields above will be
    # None rather than computed. Distinguishes "new listing, not enough
    # history yet" from "computation genuinely failed."
    insufficient_history: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_indicators(
    bars: list[PriceBar],
    *,
    reference_price: float | None = None,
    reference_time: datetime | None = None,
) -> TechnicalIndicators:
    """Compute technical indicators from a chronological list of bars.

    Parameters
    ----------
    bars:
        OHLCV bars sorted oldest-first.  Needs ≥ 20 bars for basic
        indicators; ≥ 60 for MA60.  Fewer than 5 bars (e.g. a stock/ETF
        that just listed) still returns a result — most fields come back
        None and ``insufficient_history`` is set — rather than raising,
        since a caller can legitimately want whatever partial signal
        exists (last close, support/resistance from the available highs).
    reference_price:
        Optional live price (e.g. the current quote). When supplied,
        ``support_levels_current``/``resistance_levels_current`` classify
        MA and recent high/low candidates against *this* price instead of
        the last bar close — so an MA above spot is reported as resistance,
        never support.
    reference_time:
        Timestamp of *reference_price*. When it falls on a later trading
        day than the last bar (NYSE calendar, US/Eastern date), the result
        is flagged ``indicator_stale`` with a ``stale_reason`` — the bars
        are missing one or more completed sessions relative to the quote.

    Returns
    -------
    TechnicalIndicators
        Frozen model with all computed values.

    Raises
    ------
    ValueError
        If *bars* is empty — there's nothing to compute from at all.
    """
    if not bars:
        raise ValueError("Need at least 1 bar, got 0")

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    volumes = [b.volume for b in bars]

    last_close = closes[-1]

    # ── Moving Averages ───────────────────────────────────────────────
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)

    # ── Bias ──────────────────────────────────────────────────────────
    bias_ma5 = _bias(last_close, ma5)
    bias_ma10 = _bias(last_close, ma10)
    bias_ma20 = _bias(last_close, ma20)

    if bias_ma5 is not None:
        if abs(bias_ma5) < 2:
            bias_status: Literal["safe", "caution", "danger", "unknown"] = "safe"
        elif abs(bias_ma5) < 5:
            bias_status = "caution"
        else:
            bias_status = "danger"
    else:
        bias_status = "unknown"

    # ── MA Alignment & Trend ──────────────────────────────────────────
    if ma5 is not None and ma10 is not None and ma20 is not None:
        if ma5 >= ma10 >= ma20:
            ma_alignment: Literal["bullish", "bearish", "mixed", "unknown"] = "bullish"
        elif ma5 <= ma10 <= ma20:
            ma_alignment = "bearish"
        else:
            ma_alignment = "mixed"
    else:
        ma_alignment = "unknown"

    if ma_alignment == "bullish":
        trend_status = "STRONG_BULL" if (bias_ma5 is not None and bias_ma5 < 2) else "BULL"
    elif ma_alignment == "bearish":
        trend_status = "STRONG_BEAR" if (bias_ma5 is not None and bias_ma5 > -2) else "BEAR"
    else:
        trend_status = "NEUTRAL"

    # ── Volume Ratio ──────────────────────────────────────────────────
    volume_ratio = None
    if len(volumes) >= 6:
        # 5-day MA of volume shifted by 1 (exclude current bar)
        vol_ma5 = sum(volumes[-6:-1]) / 5
        if vol_ma5 > 0:
            volume_ratio = round(volumes[-1] / vol_ma5, 2)

    # ── MACD (12, 26, 9) ─────────────────────────────────────────────
    macd_dif, macd_dea, macd_bar = None, None, None
    if len(closes) >= 26:
        ema12 = _ema_series(closes, 12)
        ema26 = _ema_series(closes, 26)
        dif_series = [a - b for a, b in zip(ema12, ema26)]
        dea_series = _ema_series(dif_series, 9)
        macd_dif = round(dif_series[-1], 4)
        macd_dea = round(dea_series[-1], 4)
        macd_bar = round(2 * (dif_series[-1] - dea_series[-1]), 4)

    # ── RSI (Wilder, 14) + direction ─────────────────────────────────
    # Direction needs one extra close: 15 closes give a single RSI value,
    # 16 give the prior bar's value to compare against.
    rsi_series = _rsi_series(closes, 14)
    rsi14 = round(rsi_series[-1], 2) if rsi_series else None
    rsi14_prev = round(rsi_series[-2], 2) if len(rsi_series) >= 2 else None
    rsi14_change: float | None = None
    rsi_direction: Literal["rising", "falling", "flat", "unknown"] = "unknown"
    if rsi14 is not None and rsi14_prev is not None:
        rsi14_change = round(rsi14 - rsi14_prev, 2)
        if rsi14_change > _RSI_FLAT_BAND:
            rsi_direction = "rising"
        elif rsi14_change < -_RSI_FLAT_BAND:
            rsi_direction = "falling"
        else:
            rsi_direction = "flat"

    # ── ATR (Wilder, 14) ─────────────────────────────────────────────
    atr14, atr_pct = None, None
    if len(closes) >= 15:
        tr_vals = _true_range(highs, lows, closes)
        atr_val = _wilder_smooth(tr_vals, 14)
        if atr_val is not None and last_close > 0:
            atr14 = round(atr_val, 4)
            atr_pct = round(atr_val / last_close * 100, 2)

    # ── Bollinger Bands (20, 2) ───────────────────────────────────────
    bb_upper, bb_lower, bb_pct_b, bb_bandwidth = None, None, None, None
    bb_result = _bollinger_bands(closes, 20, 2.0)
    if bb_result is not None:
        bb_upper_val, bb_lower_val = bb_result
        bb_upper = _r4(bb_upper_val)
        bb_lower = _r4(bb_lower_val)
        middle = (bb_upper_val + bb_lower_val) / 2
        bandwidth = bb_upper_val - bb_lower_val
        bb_bandwidth = round(bandwidth / middle * 100, 4) if middle != 0 else 0.0
        bb_pct_b = round((last_close - bb_lower_val) / bandwidth, 4) if bandwidth > 0 else None

    # ── Support / Resistance (classified vs the last bar close) ──────
    support_levels = sorted(
        [round(v, 4) for v in [ma5, ma10, ma20] if v is not None and v < last_close],
        reverse=True,
    )
    recent_highs = sorted(highs[-20:], reverse=True)[:5]
    resistance_levels = sorted(
        [round(h, 2) for h in recent_highs if h > last_close],
    )[:3]

    # ── Live-quote-aware view + staleness ─────────────────────────────
    as_of = bars[-1].date
    support_levels_current: list[float] | None = None
    resistance_levels_current: list[float] | None = None
    indicator_stale: bool | None = None
    stale_reason: str | None = None
    if reference_price is not None and reference_price > 0:
        ma_candidates = [v for v in (ma5, ma10, ma20) if v is not None]
        recent_lows = sorted(lows[-20:])[:5]
        support_levels_current = sorted(
            {round(v, 4) for v in ma_candidates + recent_lows if v < reference_price},
            reverse=True,
        )[:3]
        resistance_levels_current = sorted(
            {round(v, 4) for v in ma_candidates + recent_highs if v > reference_price},
        )[:3]
        indicator_stale = False
        if reference_time is not None:
            # NYSE trades on US/Eastern dates; a UTC date would flip to
            # "tomorrow" at 8pm ET and false-flag same-evening quotes.
            ref_date = reference_time.astimezone(_EASTERN).date()
            if ref_date > as_of and _has_trading_days_in_gap(as_of, ref_date):
                indicator_stale = True
                stale_reason = (
                    f"bars end {as_of.isoformat()} but quote is from {ref_date.isoformat()};"
                    " MA/support levels exclude the most recent session(s)"
                )

    return TechnicalIndicators(
        as_of=as_of,
        computed_at=datetime.now(UTC),
        last_close=round(last_close, 4),
        reference_price=reference_price,
        support_levels_current=support_levels_current,
        resistance_levels_current=resistance_levels_current,
        indicator_stale=indicator_stale,
        stale_reason=stale_reason,
        ma5=_r4(ma5),
        ma10=_r4(ma10),
        ma20=_r4(ma20),
        ma60=_r4(ma60),
        bias_ma5=bias_ma5,
        bias_ma10=bias_ma10,
        bias_ma20=bias_ma20,
        bias_status=bias_status,
        ma_alignment=ma_alignment,
        trend_status=trend_status,
        volume_ratio=volume_ratio,
        macd_dif=macd_dif,
        macd_dea=macd_dea,
        macd_bar=macd_bar,
        rsi14=rsi14,
        rsi14_prev=rsi14_prev,
        rsi14_change=rsi14_change,
        rsi_direction=rsi_direction,
        atr14=atr14,
        atr_pct=atr_pct,
        support_levels=support_levels,
        resistance_levels=resistance_levels,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        bb_pct_b=bb_pct_b,
        bb_bandwidth=bb_bandwidth,
        insufficient_history=len(bars) < 5,
    )


# ---------------------------------------------------------------------------
# Internal helpers — pure Python, no dependencies
# ---------------------------------------------------------------------------


def _r4(v: float | None) -> float | None:
    """Round to 4 decimals, or None."""
    return round(v, 4) if v is not None else None


def _sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last *period* values."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _ema_series(values: list[float], span: int) -> list[float]:
    """Compute EMA over the entire series, returning a list of same length.

    Uses the standard ``alpha = 2 / (span + 1)`` multiplier.
    """
    alpha = 2 / (span + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(alpha * v + (1 - alpha) * result[-1])
    return result


def _bias(price: float, ma: float | None) -> float | None:
    """Price deviation from MA, in percent."""
    if ma is not None and ma > 0:
        return round((price - ma) / ma * 100, 2)
    return None


def _rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """RSI series using Wilder smoothing (EMA with alpha = 1/period).

    Returns one unrounded value per bar from ``closes[period:]`` onward
    (empty when there is not enough history), so the caller can read both
    the current RSI (``[-1]``) and the previous bar's (``[-2]``) from a
    single pass. Values are not rounded — the caller rounds for output.
    """
    if len(closes) < period + 1:
        return []

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    alpha = 1 / period
    avg_gain = gains[0]
    avg_loss = losses[0]
    series = []
    for i in range(1, len(gains)):
        avg_gain = alpha * gains[i] + (1 - alpha) * avg_gain
        avg_loss = alpha * losses[i] + (1 - alpha) * avg_loss
        if i >= period - 1:
            series.append(100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))
    return series


def _true_range(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """Compute True Range series (length = len - 1)."""
    tr = []
    for i in range(1, len(closes)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr.append(max(hl, hc, lc))
    return tr


def _wilder_smooth(values: list[float], period: int) -> float | None:
    """Wilder smoothing (EMA with alpha = 1/period), returns last value."""
    if len(values) < period:
        return None
    alpha = 1 / period
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


def _bollinger_bands(
    closes: list[float], period: int = 20, num_std: float = 2.0
) -> tuple[float, float] | None:
    """Return (upper, lower) Bollinger Bands or None if not enough data."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = variance**0.5
    return mean + num_std * std, mean - num_std * std
