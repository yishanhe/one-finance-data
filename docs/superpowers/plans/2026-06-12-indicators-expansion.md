# Indicators Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Bollinger Bands to `compute_indicators()`, and three new client endpoints — `get_options_analytics` (symbol-level PCR), `get_short_interest`, and `get_market_sentiment` (market-wide PCR).

**Architecture:** Bollinger Bands extend the existing pure-OHLCV indicator pipeline. The three new endpoints follow the standard provider-router-cache pattern used by every other client method; `get_options_analytics` is derived (client aggregates results from existing `get_option_chain` calls). `get_short_interest` and `get_market_sentiment` add new provider methods on FMP/yfinance and route through `_cached_fetch`.

**Tech Stack:** Python 3.13, Pydantic v2, httpx, yfinance, uv/pytest, typer CLI.

---

## File Map

| File | Action | What changes |
|---|---|---|
| `onefinance/indicators/core.py` | Modify | Add `_bollinger_bands()` helper + 4 BB fields on `TechnicalIndicators` |
| `onefinance/core/models.py` | Modify | Add `OptionsAnalytics`, `ShortInterest`, `MarketSentiment` models |
| `onefinance/providers/base.py` | Modify | Add `get_short_interest` + `get_market_sentiment` stubs; update `_ENDPOINT_METHODS` |
| `onefinance/cache/manager.py` | Modify | Add `"short_interest"` and `"market_sentiment"` to `_DEFAULT_TTLS` |
| `onefinance/providers/fmp.py` | Modify | Implement `get_short_interest` + `get_market_sentiment` |
| `onefinance/providers/yfinance_provider.py` | Modify | Implement `get_short_interest` |
| `onefinance/core/client.py` | Modify | Add `get_options_analytics`, `get_short_interest`, `get_market_sentiment` |
| `onefinance/cli/app.py` | Modify | Add `options-analytics`, `short-interest`, `sentiment` commands |
| `onefinance/__init__.py` | Modify | Export new models |
| `tests/unit/test_indicators.py` | Modify | BB tests |
| `tests/unit/test_fmp_provider.py` | Modify | `get_short_interest` + `get_market_sentiment` FMP tests |
| `tests/unit/test_client.py` | Modify | `get_short_interest` + `get_market_sentiment` client tests |
| `tests/unit/test_client_options_analytics.py` | Create | `get_options_analytics` unit tests |

---

## Task 1: Bollinger Bands — `indicators/core.py`

**Files:**
- Modify: `onefinance/indicators/core.py`
- Test: `tests/unit/test_indicators.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_indicators.py`:

```python
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
        # Rising series: last bar is above the upper band
        closes = [100.0] * 19 + [200.0]  # spike at the end
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/unit/test_indicators.py::TestBollingerBands -v
```

Expected: FAIL — `TechnicalIndicators` has no `bb_upper` attribute.

- [ ] **Step 3: Add BB fields to `TechnicalIndicators` and implement**

In `onefinance/indicators/core.py`, add fields to `TechnicalIndicators` (after the ATR block):

```python
# Bollinger Bands (20, 2)
bb_upper: float | None = None
bb_lower: float | None = None
bb_pct_b: float | None = None      # (close - lower) / (upper - lower)
bb_bandwidth: float | None = None  # (upper - lower) / middle * 100
```

Add helper at the bottom of the file (before the final line):

```python
def _bollinger_bands(
    closes: list[float], period: int = 20, num_std: float = 2.0
) -> tuple[float, float] | None:
    """Return (upper, lower) Bollinger Bands or None if not enough data."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = variance ** 0.5
    return mean + num_std * std, mean - num_std * std
```

In `compute_indicators()`, add computation after the ATR block (before the support/resistance section):

```python
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
```

Update the `return TechnicalIndicators(...)` call to include:

```python
bb_upper=bb_upper,
bb_lower=bb_lower,
bb_pct_b=bb_pct_b,
bb_bandwidth=bb_bandwidth,
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_indicators.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add onefinance/indicators/core.py tests/unit/test_indicators.py
git commit -m "feat: add Bollinger Bands to compute_indicators"
```

---

## Task 2: New Models — `models.py`

**Files:**
- Modify: `onefinance/core/models.py`

- [ ] **Step 1: Add the three new models**

In `onefinance/core/models.py`, add after the `OptionChain` model:

```python
class OptionsAnalytics(FinanceModel):
    """Aggregated put/call metrics across option expirations for a symbol."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    pcr_volume: float | None = None
    pcr_oi: float | None = None
    total_put_volume: int = 0
    total_call_volume: int = 0
    total_put_oi: int = 0
    total_call_oi: int = 0
    expirations_used: int = 0
    source: str
    fetched_at: datetime


class ShortInterest(FinanceModel):
    """Short interest data for a symbol."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    short_interest: int | None = None
    short_float_pct: float | None = None
    days_to_cover: float | None = None
    settlement_date: date | None = None
    source: str
    fetched_at: datetime


class MarketSentiment(FinanceModel):
    """Market-wide put/call ratio data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pcr_equity: float | None = None
    pcr_index: float | None = None
    pcr_total: float | None = None
    as_of_date: date | None = None
    source: str
    fetched_at: datetime
```

- [ ] **Step 2: Verify models parse correctly**

```bash
uv run python -c "
from datetime import datetime, UTC, date
from onefinance.core.models import OptionsAnalytics, ShortInterest, MarketSentiment
now = datetime.now(UTC)
oa = OptionsAnalytics(symbol='AAPL', pcr_volume=0.8, pcr_oi=0.9,
    total_put_volume=100, total_call_volume=125,
    total_put_oi=500, total_call_oi=556, expirations_used=3,
    source='yfinance', fetched_at=now)
si = ShortInterest(symbol='AAPL', short_float_pct=2.5, days_to_cover=1.2,
    source='fmp', fetched_at=now)
ms = MarketSentiment(pcr_equity=0.7, pcr_total=0.85, as_of_date=date.today(),
    source='fmp', fetched_at=now)
print('OK', oa.pcr_volume, si.days_to_cover, ms.pcr_equity)
"
```

Expected: `OK 0.8 1.2 0.7`

- [ ] **Step 3: Commit**

```bash
git add onefinance/core/models.py
git commit -m "feat: add OptionsAnalytics, ShortInterest, MarketSentiment models"
```

---

## Task 3: BaseProvider Stubs + TTL Registration

**Files:**
- Modify: `onefinance/providers/base.py`
- Modify: `onefinance/cache/manager.py`

- [ ] **Step 1: Update `base.py`**

In `onefinance/providers/base.py`, add to the imports at the top:

```python
from onefinance.core.models import (
    ...  # existing imports
    MarketSentiment,
    ShortInterest,
)
```

Add to `_ENDPOINT_METHODS` dict:

```python
"short_interest": "get_short_interest",
"market_sentiment": "get_market_sentiment",
```

Add stub methods to `BaseProvider` (after the `get_earnings_calendar` stub):

```python
def get_short_interest(self, symbol: str) -> ShortInterest:
    """Fetch short interest data for *symbol*."""
    raise NotSupportedError(self.name, "short_interest")

def get_market_sentiment(self) -> MarketSentiment:
    """Fetch market-wide put/call ratio data."""
    raise NotSupportedError(self.name, "market_sentiment")
```

- [ ] **Step 2: Add TTLs to `cache/manager.py`**

In `onefinance/cache/manager.py`, add to `_DEFAULT_TTLS`:

```python
"short_interest": 86400,    # 1 day — reported bi-monthly
"market_sentiment": 14400,  # 4 hours — daily CBOE data
```

- [ ] **Step 3: Verify capability discovery works**

```bash
uv run python -c "
from onefinance.providers.fmp import FMPProvider
p = FMPProvider.__new__(FMPProvider)
p._api_key = 'test'
print('short_interest supported by base:', p.supports('short_interest'))
"
```

Expected: `short_interest supported by base: False` (not yet overridden — that's correct).

- [ ] **Step 4: Commit**

```bash
git add onefinance/providers/base.py onefinance/cache/manager.py
git commit -m "feat: add short_interest and market_sentiment stubs and TTLs"
```

---

## Task 4: YFinance `get_short_interest`

**Files:**
- Modify: `onefinance/providers/yfinance_provider.py`
- Test: `tests/unit/test_client.py` (or new file as appropriate)

- [ ] **Step 1: Write the failing test**

Add a new test file `tests/unit/test_yfinance_short_interest.py`:

```python
"""Unit tests for YFinanceProvider.get_short_interest."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from onefinance.core.models import ShortInterest
from onefinance.providers.yfinance_provider import YFinanceProvider


@pytest.fixture
def provider() -> YFinanceProvider:
    return YFinanceProvider()


class TestGetShortInterest:
    def test_returns_short_interest(self, provider: YFinanceProvider) -> None:
        mock_info = {
            "sharesShort": 50_000_000,
            "shortRatio": 2.5,
            "shortPercentOfFloat": 0.035,
            "dateShortInterest": 1717200000,  # Unix timestamp
        }
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = provider.get_short_interest("AAPL")

        assert isinstance(result, ShortInterest)
        assert result.symbol == "AAPL"
        assert result.short_interest == 50_000_000
        assert result.days_to_cover == 2.5
        assert abs(result.short_float_pct - 3.5) < 0.01  # 0.035 * 100
        assert result.settlement_date is not None
        assert result.source == "yfinance"

    def test_partial_data_returns_nones(self, provider: YFinanceProvider) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = {}

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = provider.get_short_interest("AAPL")

        assert result.short_interest is None
        assert result.days_to_cover is None
        assert result.short_float_pct is None

    def test_network_error_raises_provider_error(self, provider: YFinanceProvider) -> None:
        from onefinance.core.errors import ProviderError

        mock_ticker = MagicMock()
        mock_ticker.info = MagicMock(side_effect=Exception("network timeout"))

        with patch("yfinance.Ticker", return_value=mock_ticker):
            with pytest.raises(ProviderError):
                provider.get_short_interest("AAPL")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/unit/test_yfinance_short_interest.py -v
```

Expected: FAIL — `YFinanceProvider` has no `get_short_interest`.

- [ ] **Step 3: Implement in `yfinance_provider.py`**

In `onefinance/providers/yfinance_provider.py`, add after `get_sector_overview` (or near the end before the rate-limit methods):

```python
def get_short_interest(self, symbol: str) -> ShortInterest:
    """Fetch short interest data from yfinance ticker.info."""
    from onefinance.core.models import ShortInterest

    now = utc_now()
    sym = normalize_symbol(symbol)
    ticker = yf.Ticker(sym)

    try:
        info: dict[str, Any] = ticker.info or {}
    except Exception as exc:
        raise ProviderError(
            code="NETWORK_ERROR",
            message=f"yfinance .info failed for {symbol}: {exc}",
            provider=self.name,
            retry_safe=True,
        ) from exc

    raw_float_pct = _safe_float(info.get("shortPercentOfFloat"))
    short_float_pct = round(raw_float_pct * 100, 4) if raw_float_pct is not None else None

    settlement_date = None
    raw_date = info.get("dateShortInterest")
    if raw_date:
        try:
            from datetime import timezone
            settlement_date = datetime.fromtimestamp(raw_date, tz=timezone.utc).date()
        except (TypeError, ValueError, OSError):
            pass

    return ShortInterest(
        symbol=sym,
        short_interest=_safe_int(info.get("sharesShort")),
        short_float_pct=short_float_pct,
        days_to_cover=_safe_float(info.get("shortRatio")),
        settlement_date=settlement_date,
        source=_SOURCE,
        fetched_at=now,
    )
```

Also add `ShortInterest` to the import of models in `yfinance_provider.py` if it imports from `models` at the top (check the existing import block; the import inside the method body also works).

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_yfinance_short_interest.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add onefinance/providers/yfinance_provider.py tests/unit/test_yfinance_short_interest.py
git commit -m "feat: implement YFinanceProvider.get_short_interest"
```

---

## Task 5: FMP `get_short_interest`

**Files:**
- Modify: `onefinance/providers/fmp.py`
- Test: `tests/unit/test_fmp_provider.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_fmp_provider.py`:

```python
class TestGetShortInterest:
    def test_returns_short_interest(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "symbol": "AAPL",
                "date": "2026-05-31",
                "shortVolume": 15_000_000,
                "shortExemptVolume": 100_000,
                "totalVolume": 60_000_000,
                "shortFloat": "2.50%",
                "shortRatio": "1.20",
                "sharesShort": 50_000_000,
            }
        ]
        resp = _mock_response(mock_data)
        with patch.object(provider._client, "get", return_value=resp):
            result = provider.get_short_interest("AAPL")

        from onefinance.core.models import ShortInterest
        assert isinstance(result, ShortInterest)
        assert result.symbol == "AAPL"
        assert result.short_interest == 50_000_000
        assert abs(result.short_float_pct - 2.50) < 0.01
        assert abs(result.days_to_cover - 1.20) < 0.01
        assert result.settlement_date is not None

    def test_empty_response_raises(self, provider: FMPProvider) -> None:
        from onefinance.core.errors import ProviderError
        resp = _mock_response([])
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError):
                provider.get_short_interest("AAPL")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/unit/test_fmp_provider.py::TestGetShortInterest -v
```

Expected: FAIL — `FMPProvider` has no `get_short_interest`.

- [ ] **Step 3: Implement in `fmp.py`**

In `onefinance/providers/fmp.py`, add after `get_forward_estimates`:

```python
def get_short_interest(self, symbol: str) -> ShortInterest:
    """Fetch short interest via FMP ``/v3/short-float-symbol/{symbol}``."""
    from onefinance.core.models import ShortInterest

    now = utc_now()
    sym = normalize_symbol(symbol)

    data = self._get(f"short-float-symbol/{sym}")

    if not data or not isinstance(data, list) or len(data) == 0:
        raise ProviderError(
            code="DATA_NOT_FOUND",
            message=f"No short interest data for {symbol}",
            provider=_SOURCE,
            retry_safe=False,
        )

    item = data[0]

    # FMP returns shortFloat as "2.50%" string — parse it
    def _parse_pct(val: Any) -> float | None:
        if val is None:
            return None
        try:
            s = str(val).replace("%", "").strip()
            return float(s)
        except (ValueError, TypeError):
            return None

    settlement_date = None
    date_str = item.get("date")
    if date_str:
        try:
            settlement_date = parse_iso_date(date_str)
        except (ValueError, TypeError):
            pass

    return ShortInterest(
        symbol=sym,
        short_interest=_safe_int(item.get("sharesShort")),
        short_float_pct=_parse_pct(item.get("shortFloat")),
        days_to_cover=_safe_float(item.get("shortRatio")),
        settlement_date=settlement_date,
        source=_SOURCE,
        fetched_at=now,
    )
```

Note: The FMP endpoint for this is `/v3/short-float-symbol/{symbol}`. Verify at https://site.financialmodelingprep.com/developer/docs during implementation if the response shape differs.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_fmp_provider.py::TestGetShortInterest -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add onefinance/providers/fmp.py tests/unit/test_fmp_provider.py
git commit -m "feat: implement FMPProvider.get_short_interest"
```

---

## Task 6: FMP `get_market_sentiment`

**Files:**
- Modify: `onefinance/providers/fmp.py`
- Test: `tests/unit/test_fmp_provider.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_fmp_provider.py`:

```python
class TestGetMarketSentiment:
    def test_returns_market_sentiment(self, provider: FMPProvider) -> None:
        mock_data = [
            {
                "date": "2026-06-12",
                "putCallRatio": 0.85,
                "putCallRatioEquity": 0.72,
                "putCallRatioIndex": 1.10,
            }
        ]
        resp = _mock_response(mock_data)
        with patch.object(provider._client, "get", return_value=resp):
            result = provider.get_market_sentiment()

        from onefinance.core.models import MarketSentiment
        assert isinstance(result, MarketSentiment)
        assert abs(result.pcr_total - 0.85) < 0.001
        assert abs(result.pcr_equity - 0.72) < 0.001
        assert abs(result.pcr_index - 1.10) < 0.001
        assert result.as_of_date is not None

    def test_empty_response_raises(self, provider: FMPProvider) -> None:
        from onefinance.core.errors import ProviderError
        resp = _mock_response([])
        with patch.object(provider._client, "get", return_value=resp):
            with pytest.raises(ProviderError):
                provider.get_market_sentiment()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/unit/test_fmp_provider.py::TestGetMarketSentiment -v
```

Expected: FAIL.

- [ ] **Step 3: Implement in `fmp.py`**

In `onefinance/providers/fmp.py`, add after `get_short_interest`:

```python
def get_market_sentiment(self) -> MarketSentiment:
    """Fetch market-wide put/call ratio via FMP ``/v3/put_call_ratio``."""
    from onefinance.core.models import MarketSentiment

    now = utc_now()

    data = self._get("put_call_ratio")

    if not data or not isinstance(data, list) or len(data) == 0:
        raise ProviderError(
            code="DATA_NOT_FOUND",
            message="No market sentiment data available",
            provider=_SOURCE,
            retry_safe=False,
        )

    item = data[0]

    as_of_date = None
    date_str = item.get("date")
    if date_str:
        try:
            as_of_date = parse_iso_date(date_str)
        except (ValueError, TypeError):
            pass

    return MarketSentiment(
        pcr_equity=_safe_float(item.get("putCallRatioEquity")),
        pcr_index=_safe_float(item.get("putCallRatioIndex")),
        pcr_total=_safe_float(item.get("putCallRatio")),
        as_of_date=as_of_date,
        source=_SOURCE,
        fetched_at=now,
    )
```

> **Note:** FMP's market PCR endpoint path may differ. Check https://site.financialmodelingprep.com/developer/docs for the exact path. If FMP does not offer market PCR, replace with `/v3/fear-greed-index` and map `fgi` (numeric 0–100) to `pcr_total` as `None` and add a `fear_greed_index: float | None` field to `MarketSentiment` instead. Update the model, test, and CLI accordingly if this happens.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_fmp_provider.py::TestGetMarketSentiment -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add onefinance/providers/fmp.py tests/unit/test_fmp_provider.py
git commit -m "feat: implement FMPProvider.get_market_sentiment"
```

---

## Task 7: Client `get_options_analytics`

**Files:**
- Modify: `onefinance/core/client.py`
- Create: `tests/unit/test_client_options_analytics.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_client_options_analytics.py`:

```python
"""Tests for OneFinanceClient.get_options_analytics."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest

from onefinance.core.client import OneFinanceClient
from onefinance.core.models import OptionChain, OptionContract, OptionsAnalytics


def _contract(
    strike: float,
    volume: int | None,
    open_interest: int | None,
) -> OptionContract:
    now = datetime.now(UTC)
    return OptionContract(
        contract_symbol=f"TEST{strike}",
        strike=strike,
        volume=volume,
        open_interest=open_interest,
        source="yfinance",
        fetched_at=now,
    )


def _chain(calls_vol: int, puts_vol: int, calls_oi: int, puts_oi: int) -> OptionChain:
    now = datetime.now(UTC)
    return OptionChain(
        symbol="AAPL",
        expiration_date=date(2026, 7, 18),
        calls=[_contract(150.0, calls_vol, calls_oi)],
        puts=[_contract(140.0, puts_vol, puts_oi)],
        source="yfinance",
        fetched_at=now,
    )


class TestGetOptionsAnalytics:
    def test_aggregates_pcr_across_expirations(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18), date(2026, 8, 15), date(2026, 9, 19)]
        chains = [
            _chain(calls_vol=100, puts_vol=80, calls_oi=500, puts_oi=400),
            _chain(calls_vol=200, puts_vol=160, calls_oi=1000, puts_oi=800),
            _chain(calls_vol=300, puts_vol=240, calls_oi=1500, puts_oi=1200),
        ]

        with patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations):
            with patch.object(OneFinanceClient, "get_option_chain", side_effect=chains):
                result = client.get_options_analytics("AAPL")

        assert isinstance(result, OptionsAnalytics)
        assert result.total_call_volume == 600
        assert result.total_put_volume == 480
        assert abs(result.pcr_volume - 480 / 600) < 0.001
        assert result.expirations_used == 3

    def test_caps_at_max_expirations(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18) + __import__('datetime').timedelta(days=30 * i) for i in range(10)]
        chain = _chain(calls_vol=100, puts_vol=80, calls_oi=500, puts_oi=400)

        with patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations):
            with patch.object(OneFinanceClient, "get_option_chain", return_value=chain) as mock_chain:
                result = client.get_options_analytics("AAPL", max_expirations=3)

        assert mock_chain.call_count == 3
        assert result.expirations_used == 3

    def test_pcr_none_when_no_call_volume(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18)]
        chain = _chain(calls_vol=0, puts_vol=50, calls_oi=0, puts_oi=100)

        with patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations):
            with patch.object(OneFinanceClient, "get_option_chain", return_value=chain):
                result = client.get_options_analytics("AAPL")

        assert result.pcr_volume is None
        assert result.pcr_oi is None

    def test_none_volumes_treated_as_zero(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18)]
        # OptionContract with None volume
        now = datetime.now(UTC)
        chain = OptionChain(
            symbol="AAPL",
            expiration_date=date(2026, 7, 18),
            calls=[_contract(150.0, None, None)],
            puts=[_contract(140.0, None, None)],
            source="yfinance",
            fetched_at=now,
        )

        with patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations):
            with patch.object(OneFinanceClient, "get_option_chain", return_value=chain):
                result = client.get_options_analytics("AAPL")

        assert result.total_call_volume == 0
        assert result.total_put_volume == 0
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/unit/test_client_options_analytics.py -v
```

Expected: FAIL — `OneFinanceClient` has no `get_options_analytics`.

- [ ] **Step 3: Implement in `client.py`**

First, add `UTC` to the `datetime` import in `onefinance/core/client.py` (line ~14):

```python
from datetime import UTC, date, datetime, timedelta
```

Then add the method in `onefinance/core/client.py` after `get_option_chain`:

```python
def get_options_analytics(
    self,
    symbol: str,
    max_expirations: int = 6,
    *,
    no_cache: bool = False,
    provider: str | None = None,
) -> OptionsAnalytics:
    """Aggregate put/call ratio and open interest across option expirations.

    Fetches the nearest *max_expirations* option chains concurrently and
    aggregates puts and calls volume and open interest.  Relies on the
    per-chain cache (5-min TTL) rather than caching the derived result.
    """
    import concurrent.futures

    from onefinance.core.models import OptionsAnalytics

    sym = symbol.upper()
    expirations = self.get_options_expirations(sym, no_cache=no_cache, provider=provider)
    selected = sorted(expirations)[:max_expirations]

    chains: list[OptionChain] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(selected), 6)) as executor:
        futures = {
            executor.submit(
                self.get_option_chain, sym, exp, no_cache=no_cache, provider=provider
            ): exp
            for exp in selected
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                chains.append(future.result())
            except Exception:
                pass  # partial failure — skip this expiration

    total_call_vol = sum(c.volume or 0 for ch in chains for c in ch.calls)
    total_put_vol = sum(c.volume or 0 for ch in chains for c in ch.puts)
    total_call_oi = sum(c.open_interest or 0 for ch in chains for c in ch.calls)
    total_put_oi = sum(c.open_interest or 0 for ch in chains for c in ch.puts)

    pcr_volume = round(total_put_vol / total_call_vol, 4) if total_call_vol > 0 else None
    pcr_oi = round(total_put_oi / total_call_oi, 4) if total_call_oi > 0 else None

    source = chains[0].source if chains else "unknown"

    return OptionsAnalytics(
        symbol=sym,
        pcr_volume=pcr_volume,
        pcr_oi=pcr_oi,
        total_put_volume=total_put_vol,
        total_call_volume=total_call_vol,
        total_put_oi=total_put_oi,
        total_call_oi=total_call_oi,
        expirations_used=len(chains),
        source=source,
        fetched_at=datetime.now(UTC),
    )
```

`OptionChain` is already imported at the top of `client.py` (no change needed).

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_client_options_analytics.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add onefinance/core/client.py tests/unit/test_client_options_analytics.py
git commit -m "feat: add get_options_analytics to OneFinanceClient"
```

---

## Task 8: Client `get_short_interest` + `get_market_sentiment`

**Files:**
- Modify: `onefinance/core/client.py`
- Test: `tests/unit/test_client.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_client.py`. Also add `ShortInterest, MarketSentiment` to the `from onefinance.core.models import (...)` block at the top.

```python
class TestGetShortInterest:
    def test_returns_result_from_cached_fetch(self) -> None:
        expected = ShortInterest(
            symbol="AAPL",
            short_interest=50_000_000,
            short_float_pct=2.5,
            days_to_cover=1.2,
            settlement_date=None,
            source="fmp",
            fetched_at=NOW,
        )
        client = OneFinanceClient.__new__(OneFinanceClient)
        with patch.object(OneFinanceClient, "_cached_fetch", return_value=expected):
            result = client.get_short_interest("AAPL")
        assert isinstance(result, ShortInterest)
        assert result.symbol == "AAPL"
        assert result.short_float_pct == 2.5

    def test_symbol_uppercased(self) -> None:
        expected = ShortInterest(
            symbol="AAPL", source="fmp", fetched_at=NOW
        )
        client = OneFinanceClient.__new__(OneFinanceClient)
        with patch.object(OneFinanceClient, "_cached_fetch", return_value=expected) as mock_cf:
            client.get_short_interest("aapl")
        call_kwargs = mock_cf.call_args.kwargs
        assert call_kwargs["symbol"] == "AAPL"


class TestGetMarketSentiment:
    def test_returns_result_from_cached_fetch(self) -> None:
        expected = MarketSentiment(
            pcr_equity=0.72,
            pcr_index=1.10,
            pcr_total=0.85,
            as_of_date=None,
            source="fmp",
            fetched_at=NOW,
        )
        client = OneFinanceClient.__new__(OneFinanceClient)
        with patch.object(OneFinanceClient, "_cached_fetch", return_value=expected):
            result = client.get_market_sentiment()
        assert isinstance(result, MarketSentiment)
        assert result.pcr_total == 0.85

    def test_uses_market_sentiment_endpoint(self) -> None:
        expected = MarketSentiment(source="fmp", fetched_at=NOW)
        client = OneFinanceClient.__new__(OneFinanceClient)
        with patch.object(OneFinanceClient, "_cached_fetch", return_value=expected) as mock_cf:
            client.get_market_sentiment()
        assert mock_cf.call_args.kwargs["endpoint"] == "market_sentiment"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/unit/test_client.py::TestGetShortInterest tests/unit/test_client.py::TestGetMarketSentiment -v
```

Expected: FAIL.

- [ ] **Step 3: Implement both methods in `client.py`**

In `onefinance/core/client.py`, add after `get_options_analytics`:

```python
def get_short_interest(
    self,
    symbol: str,
    *,
    ttl: int | None = None,
    no_cache: bool = False,
    provider: str | None = None,
) -> ShortInterest:
    """Fetch short interest and days-to-cover for *symbol*."""
    from onefinance.core.models import ShortInterest

    cache_key = make_key("short_interest", symbol=symbol.upper())
    effective_ttl = ttl if ttl is not None else self._default_ttl("short_interest")

    return self._cached_fetch(
        cache_key=cache_key,
        endpoint="short_interest",
        ttl=effective_ttl,
        no_cache=no_cache,
        provider_name=provider,
        symbol=symbol.upper(),
        fetch_fn=lambda p: p.get_short_interest(symbol.upper()),
    )

def get_market_sentiment(
    self,
    *,
    ttl: int | None = None,
    no_cache: bool = False,
    provider: str | None = None,
) -> MarketSentiment:
    """Fetch market-wide put/call ratio data."""
    from onefinance.core.models import MarketSentiment

    cache_key = make_key("market_sentiment")
    effective_ttl = ttl if ttl is not None else self._default_ttl("market_sentiment")

    return self._cached_fetch(
        cache_key=cache_key,
        endpoint="market_sentiment",
        ttl=effective_ttl,
        no_cache=no_cache,
        provider_name=provider,
        fetch_fn=lambda p: p.get_market_sentiment(),
    )
```

Note: `_cached_fetch` passes `symbol` as a keyword arg to the router for logging — `get_market_sentiment` has no symbol, so omit that kwarg (check the method signature in client.py to confirm it's optional).

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_client.py::TestGetShortInterest tests/unit/test_client.py::TestGetMarketSentiment -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add onefinance/core/client.py tests/unit/test_client.py
git commit -m "feat: add get_short_interest and get_market_sentiment to OneFinanceClient"
```

---

## Task 9: CLI Commands

**Files:**
- Modify: `onefinance/cli/app.py`

- [ ] **Step 1: Add `options-analytics` command**

In `onefinance/cli/app.py`, add after the existing `options` command:

```python
@app.command()
def options_analytics(
    symbol: str = typer.Argument(...),
    max_expirations: int = typer.Option(6, "--max-expirations", "-n", help="Max expiration dates to aggregate."),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """Fetch aggregated put/call ratio and open interest for SYMBOL."""
    try:
        client = _make_client(config)
        result = client.get_options_analytics(
            symbol, max_expirations=max_expirations, no_cache=no_cache, provider=provider
        )
        data = result.model_dump(mode="json")
        _emit(
            make_envelope(
                "options_analytics",
                data,
                {"symbol": symbol.upper(), "expirations_used": result.expirations_used, "source": result.source},
            ),
            fmt,
        )
    except FinanceError as exc:
        _error_exit("options-analytics", exc)
```

- [ ] **Step 2: Add `short-interest` command**

```python
@app.command()
def short_interest(
    symbol: str = typer.Argument(...),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """Fetch short interest and days-to-cover for SYMBOL."""
    try:
        client = _make_client(config)
        result = client.get_short_interest(symbol, no_cache=no_cache, provider=provider)
        data = result.model_dump(mode="json")
        _emit(
            make_envelope(
                "short_interest",
                data,
                {"symbol": symbol.upper(), "source": result.source},
            ),
            fmt,
        )
    except FinanceError as exc:
        _error_exit("short-interest", exc)
```

- [ ] **Step 3: Add `sentiment` command**

```python
@app.command()
def sentiment(
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """Fetch market-wide put/call ratio data."""
    try:
        client = _make_client(config)
        result = client.get_market_sentiment(no_cache=no_cache, provider=provider)
        data = result.model_dump(mode="json")
        _emit(
            make_envelope(
                "market_sentiment",
                data,
                {"source": result.source},
            ),
            fmt,
        )
    except FinanceError as exc:
        _error_exit("sentiment", exc)
```

- [ ] **Step 4: Verify CLI commands are registered**

```bash
uv run ofclient --help
```

Expected: `options-analytics`, `short-interest`, `sentiment` appear in the command list.

```bash
uv run ofclient options-analytics --help
uv run ofclient short-interest --help
uv run ofclient sentiment --help
```

Expected: Each shows correct usage.

- [ ] **Step 5: Commit**

```bash
git add onefinance/cli/app.py
git commit -m "feat: add options-analytics, short-interest, sentiment CLI commands"
```

---

## Task 10: Export New Models from `__init__.py`

**Files:**
- Modify: `onefinance/__init__.py`

- [ ] **Step 1: Add imports and `__all__` entries**

In `onefinance/__init__.py`, add to the `from onefinance.core.models import (...)` block:

```python
    MarketSentiment,
    OptionsAnalytics,
    ShortInterest,
```

Add to `__all__`:

```python
    "MarketSentiment",
    "OptionsAnalytics",
    "ShortInterest",
```

- [ ] **Step 2: Verify exports**

```bash
uv run python -c "from onefinance import OptionsAnalytics, ShortInterest, MarketSentiment; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add onefinance/__init__.py
git commit -m "feat: export OptionsAnalytics, ShortInterest, MarketSentiment from package"
```

---

## Task 11: Full Test Suite + Lint

- [ ] **Step 1: Run all unit tests**

```bash
uv run pytest tests/unit/ -v
```

Expected: All pass, no failures.

- [ ] **Step 2: Run lint and type-check**

```bash
uv run ruff check onefinance tests
uv run ruff format onefinance tests
uv run mypy onefinance tests
```

Fix any issues before proceeding.

- [ ] **Step 3: Commit any lint fixes**

```bash
git add -u
git commit -m "fix: lint and type errors from indicators expansion"
```

---

## Task 12: Integration Tests

- [ ] **Step 1: Add integration tests to `tests/integration/test_fmp_live.py`**

Add to `tests/integration/test_fmp_live.py`:

```python
@pytest.mark.integration
def test_get_short_interest_live() -> None:
    from onefinance.providers.fmp import FMPProvider
    from onefinance.core.models import ShortInterest

    p = FMPProvider()
    result = p.get_short_interest("AAPL")
    assert isinstance(result, ShortInterest)
    assert result.symbol == "AAPL"


@pytest.mark.integration
def test_get_market_sentiment_live() -> None:
    from onefinance.providers.fmp import FMPProvider
    from onefinance.core.models import MarketSentiment

    p = FMPProvider()
    result = p.get_market_sentiment()
    assert isinstance(result, MarketSentiment)
    # At least one PCR field should be populated
    assert any(v is not None for v in [result.pcr_equity, result.pcr_index, result.pcr_total])
```

- [ ] **Step 2: Add integration test for `get_options_analytics` to yfinance live tests (if file exists)**

If `tests/integration/` has a yfinance live test file, add:

```python
@pytest.mark.integration
def test_get_options_analytics_live() -> None:
    from onefinance.core.client import OneFinanceClient
    from onefinance.core.models import OptionsAnalytics

    client = OneFinanceClient()
    result = client.get_options_analytics("AAPL", max_expirations=2)
    assert isinstance(result, OptionsAnalytics)
    assert result.symbol == "AAPL"
    assert result.expirations_used <= 2
    assert result.total_call_volume >= 0
```

- [ ] **Step 3: Final commit**

```bash
git add tests/integration/
git commit -m "test: add integration tests for short_interest, market_sentiment, options_analytics"
```
