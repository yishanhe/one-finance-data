# Indicators Expansion Design

**Date:** 2026-06-12  
**Status:** Approved

## Scope

Five additions across two categories:

**Category A — OHLCV extension**
- Bollinger Bands (%B, bandwidth) — extend existing `compute_indicators()`

**Category B — Sentiment / multi-source**
- Put-Call Ratio (symbol-level) — derived from options chain
- Short Interest / Days-to-Cover — new provider endpoint
- Put-Call Ratio (market-wide) — new provider endpoint

IV Rank / IV Percentile explicitly deferred (requires 52-week historical IV data not currently available).

---

## Architecture

Approach: separate endpoints per concern (matches existing 20+ endpoint pattern).

```
OneFinanceClient
  ├── get_indicators(symbol)          # extended: adds BB fields
  ├── get_options_analytics(symbol)   # new: derived, client-side aggregation
  ├── get_short_interest(symbol)      # new: FMP + YFinance
  └── get_market_sentiment()          # new: FMP only
```

---

## Section 1 — Bollinger Bands

**Location:** `onefinance/indicators/core.py`

New helper `_bollinger_bands(closes, period=20, num_std=2)` returns `(upper, lower)`.

New fields on `TechnicalIndicators`:

```python
bb_upper: float | None = None
bb_lower: float | None = None
bb_pct_b: float | None = None      # (close - lower) / (upper - lower)
bb_bandwidth: float | None = None  # (upper - lower) / middle * 100
```

- Requires ≥ 20 bars (same threshold as MA20)
- `bb_middle` is MA20 (already computed as `ma20`)
- %B < 0 = below lower band; %B > 1 = above upper band

---

## Section 2 — Options Analytics (symbol-level PCR)

**New model** (`models.py`):

```python
class OptionsAnalytics(FinanceModel):
    symbol: str
    pcr_volume: float | None        # sum(put vol) / sum(call vol)
    pcr_oi: float | None            # sum(put OI) / sum(call OI)
    total_put_volume: int
    total_call_volume: int
    total_put_oi: int
    total_call_oi: int
    expirations_used: int
    source: str
    fetched_at: datetime
```

**Client method:** `get_options_analytics(symbol, max_expirations=6)`

Implementation (client-side derived, no provider method):
1. `get_options_expirations(symbol)` → sort ascending, slice `[:max_expirations]`
2. Fan out `get_option_chain(symbol, expiration)` concurrently via `ThreadPoolExecutor`
3. Aggregate puts/calls volume and OI across all chains
4. Return `OptionsAnalytics`

- **TTL:** 5 minutes (matches `get_option_chain`)
- **Cache key:** `make_key("options_analytics", symbol=symbol, max_expirations=max_expirations)`
- **Provider support:** YFinance only (sole options provider)
- `pcr_volume`/`pcr_oi` are `None` if denominator (call vol/OI) is zero

---

## Section 3 — Short Interest

**New model** (`models.py`):

```python
class ShortInterest(FinanceModel):
    symbol: str
    short_interest: int | None      # shares short
    short_float_pct: float | None   # % of float shorted
    days_to_cover: float | None     # short interest / avg daily volume
    settlement_date: date | None
    source: str
    fetched_at: datetime
```

**Client method:** `get_short_interest(symbol)`

- **TTL:** 1 day
- **Provider support:**
  - `FMPProvider`: `/v3/short-float-symbol/{symbol}`
  - `YFinanceProvider`: `ticker.info` fields (`sharesShort`, `shortRatio`, `shortPercentOfFloat`)
- Router tries FMP first, falls back to YFinance

---

## Section 4 — Market Sentiment (market-wide PCR)

**New model** (`models.py`):

```python
class MarketSentiment(FinanceModel):
    pcr_equity: float | None        # equity-only put/call ratio
    pcr_index: float | None         # index options put/call ratio
    pcr_total: float | None         # combined put/call ratio
    as_of_date: date | None
    source: str
    fetched_at: datetime
```

**Client method:** `get_market_sentiment()`

- **TTL:** 4 hours
- **Cache key:** `make_key("market_sentiment")` (no params)
- **Provider support:** FMP only
  - Exact endpoint confirmed during implementation (`/v3/put_call_ratio_market` or `/v4/market-fear-and-greed-index`)
- No symbol param — single global result
- Returns `NotSupportedError` from all non-FMP providers

---

## Section 5 — Provider Capability Matrix (additions)

| Endpoint | FMP | Finnhub | Twelve Data | YFinance |
|---|---|---|---|---|
| `get_options_analytics` | — | — | — | ✓ (derived) |
| `get_short_interest` | ✓ | — | — | ✓ |
| `get_market_sentiment` | ✓ | — | — | — |

---

## Section 6 — CLI Commands

New commands in `cli/app.py`:

```
ofclient options-analytics AAPL
ofclient options-analytics AAPL --max-expirations 6
ofclient short-interest AAPL
ofclient sentiment
```

All support `--format json|table|csv` via existing output machinery.

---

## Error Handling

- `get_options_analytics`: if all chain fetches fail, raises `AllProvidersFailedError`. If some fail, aggregates from successful ones and sets `expirations_used` accordingly.
- `get_short_interest` / `get_market_sentiment`: standard provider routing — `NotSupportedError` skipped, errors collected, `AllProvidersFailedError` if all fail.
- Zero-denominator PCR (no call volume/OI): field set to `None`, not raise.

---

## Testing

- Unit tests for `_bollinger_bands()` helper (known values)
- Unit tests for `OptionsAnalytics` aggregation logic (mock chains)
- Unit tests for `ShortInterest` / `MarketSentiment` model parsing
- Integration tests (marked `@pytest.mark.integration`) for all three new client methods
- CLI snapshot tests for new commands
