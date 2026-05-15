# M6+M7 Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `FinnhubProvider` (all 7 endpoints) and `TwelveDataProvider` (price history + quote), following the exact pattern of the existing `FMPProvider`.

**Architecture:** Each provider subclasses `BaseProvider`, implements a `_get()` HTTP helper using `httpx.Client`, normalizes raw API responses to Pydantic models, and raises `RateLimitError` on quota signals. All tests mock `provider._client.get` via `patch.object`. Provider instances are passed directly to `OneFinanceClient(providers=[...])`.

**Tech Stack:** Python 3.11+, httpx, Pydantic v2, pytest, unittest.mock

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `onefinance/providers/finnhub.py` | Create | Finnhub API adapter — all 7 endpoints |
| `onefinance/providers/twelve_data.py` | Create | Twelve Data adapter — price_history + quote |
| `tests/unit/test_finnhub_provider.py` | Create | 28 unit tests for FinnhubProvider |
| `tests/unit/test_twelve_data_provider.py` | Create | 15 unit tests for TwelveDataProvider |

---

## Task 1: FinnhubProvider skeleton, constructor, rate-limit detection

**Files:**
- Create: `onefinance/providers/finnhub.py`
- Create: `tests/unit/test_finnhub_provider.py`

- [ ] **Step 1: Write failing constructor tests**

```python
# tests/unit/test_finnhub_provider.py
"""Unit tests for FinnhubProvider with mocked httpx responses."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from onefinance.core.errors import ConfigError, ProviderError, RateLimitError
from onefinance.core.models import (
    CompanyInfo, EarningsRecord, FinancialRatios,
    IncomeStatement, BalanceSheet, CashFlow,
    InsiderTrade, PriceBar, Quote,
)
from onefinance.providers.finnhub import FinnhubProvider


@pytest.fixture
def provider() -> FinnhubProvider:
    return FinnhubProvider(api_key="test_key", timeout=5)


def _mock_response(data: Any, status_code: int = 200, headers: dict | None = None) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data) if not isinstance(data, str) else data
    resp.headers = headers or {}
    return resp


class TestConstructor:
    def test_api_key_from_param(self):
        p = FinnhubProvider(api_key="my_key")
        assert p._api_key == "my_key"

    def test_api_key_from_env(self):
        with patch.dict("os.environ", {"FINNHUB_API_KEY": "env_key"}):
            p = FinnhubProvider()
            assert p._api_key == "env_key"

    def test_missing_key_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ConfigError):
                FinnhubProvider()

    def test_name(self):
        p = FinnhubProvider(api_key="k")
        assert p.name == "finnhub"


class TestRateLimitDetection:
    def test_http_429_raises_rate_limit(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({}, 429, {"Retry-After": "120"})):
            with pytest.raises(RateLimitError) as exc_info:
                provider._get("quote", {"symbol": "AAPL"})
        assert exc_info.value.retry_after_seconds == 120

    def test_http_429_default_cooldown(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({}, 429, {})):
            with pytest.raises(RateLimitError) as exc_info:
                provider._get("quote", {})
        assert exc_info.value.retry_after_seconds == 60

    def test_non_200_raises_provider_error(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response("Server Error", 500)):
            with pytest.raises(ProviderError):
                provider._get("quote", {})
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /Users/admin/projects/one-finance-data
python -m pytest tests/unit/test_finnhub_provider.py::TestConstructor -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'onefinance.providers.finnhub'`

- [ ] **Step 3: Create the provider skeleton**

```python
# onefinance/providers/finnhub.py
"""Finnhub provider adapter.

Uses the Finnhub REST API (https://finnhub.io/api/v1).
Requires an API key set via the ``FINNHUB_API_KEY`` environment variable.

Free tier: 60 calls/minute. Realtime quotes on free tier (some 20-min delay).

See design doc §6, §7, §9 for the provider contract.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import httpx

from onefinance.core.errors import ConfigError, ProviderError, RateLimitError
from onefinance.core.models import (
    BalanceSheet, CashFlow, CompanyInfo, EarningsRecord,
    FinancialRatios, IncomeStatement, InsiderTrade, PriceBar, Quote,
)
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "finnhub"
_BASE_URL = "https://finnhub.io/api/v1"


class FinnhubProvider(BaseProvider):
    """Provider adapter for Finnhub.

    Parameters
    ----------
    api_key:
        Finnhub API key. If ``None``, reads from ``FINNHUB_API_KEY`` env var.
    timeout:
        HTTP request timeout in seconds.
    base_url:
        Override the base URL (useful for testing).
    """

    name = _SOURCE

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 10,
        base_url: str = _BASE_URL,
    ) -> None:
        self._api_key = api_key or os.environ.get("FINNHUB_API_KEY")
        if not self._api_key:
            raise ConfigError(
                "FINNHUB_API_KEY not set. Set it in your environment or pass api_key="
            )
        self._timeout = timeout
        self._base_url = base_url
        self._client = httpx.Client(timeout=timeout)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Authenticated GET to Finnhub API.

        Raises ``RateLimitError`` on HTTP 429,
        ``ProviderError`` on other failures.
        """
        url = f"{self._base_url}/{path}"
        req_params = dict(params or {})
        req_params["token"] = self._api_key

        try:
            resp = self._client.get(url, params=req_params)
        except httpx.HTTPError as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"Finnhub request failed: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if resp.status_code == 429:
            try:
                retry_after = int(resp.headers.get("Retry-After", "60"))
            except (ValueError, TypeError):
                retry_after = 60
            raise RateLimitError(
                provider=self.name,
                message="Finnhub rate limit hit (HTTP 429)",
                retry_after_seconds=retry_after,
            )

        if resp.status_code != 200:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"Finnhub HTTP {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                retry_safe=resp.status_code >= 500,
            )

        return resp.json()

    def is_rate_limited(self, response: Any) -> bool:
        if isinstance(response, httpx.Response):
            return response.status_code == 429
        if isinstance(response, Exception):
            return "429" in str(response)
        return False

    def cooldown_for(self, response: Any) -> float:
        if isinstance(response, httpx.Response):
            try:
                return float(response.headers.get("Retry-After", "60"))
            except (ValueError, TypeError):
                pass
        return 60.0
```

- [ ] **Step 4: Run constructor + rate-limit tests**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestConstructor tests/unit/test_finnhub_provider.py::TestRateLimitDetection -v
```

Expected: 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add onefinance/providers/finnhub.py tests/unit/test_finnhub_provider.py
git commit -m "feat(m6): FinnhubProvider skeleton with constructor and rate-limit detection"
```

---

## Task 2: FinnhubProvider.get_price_history

**Files:**
- Modify: `onefinance/providers/finnhub.py`
- Modify: `tests/unit/test_finnhub_provider.py`

Finnhub `/stock/candle` accepts Unix timestamps and returns parallel arrays:
`{"c": [close,...], "h": [high,...], "l": [low,...], "o": [open,...], "t": [unix_ts,...], "v": [volume,...], "s": "ok"}`.
When no data: `{"s": "no_data"}`.

- [ ] **Step 1: Add tests**

```python
# Append to tests/unit/test_finnhub_provider.py

class TestGetPriceHistory:
    _candle_data = {
        "c": [185.64, 186.01],
        "h": [186.50, 186.90],
        "l": [184.00, 185.20],
        "o": [184.50, 185.64],
        "t": [1704153600, 1704240000],  # 2024-01-02, 2024-01-03
        "v": [52000000, 48000000],
        "s": "ok",
    }

    def test_returns_price_bars(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._candle_data)):
            bars = provider.get_price_history(
                "AAPL", date(2024, 1, 2), date(2024, 1, 3)
            )
        assert len(bars) == 2
        assert all(isinstance(b, PriceBar) for b in bars)
        assert bars[0].symbol == "AAPL"
        assert bars[0].close == 185.64
        assert bars[0].volume == 52000000
        assert bars[0].source == "finnhub"

    def test_no_data_returns_empty(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({"s": "no_data"})):
            bars = provider.get_price_history(
                "AAPL", date(2024, 1, 2), date(2024, 1, 3)
            )
        assert bars == []

    def test_symbol_uppercased(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._candle_data)):
            bars = provider.get_price_history(
                "aapl", date(2024, 1, 2), date(2024, 1, 3)
            )
        assert bars[0].symbol == "AAPL"

    def test_adj_close_equals_close(self, provider):
        # Finnhub candle does not return adjusted close; adj_close mirrors close
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._candle_data)):
            bars = provider.get_price_history(
                "AAPL", date(2024, 1, 2), date(2024, 1, 3)
            )
        assert bars[0].adj_close == bars[0].close
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetPriceHistory -v 2>&1 | head -15
```

Expected: `AttributeError: 'FinnhubProvider' object has no attribute 'get_price_history'` (base raises `NotSupportedError`)

- [ ] **Step 3: Implement get_price_history**

```python
# Add to FinnhubProvider in onefinance/providers/finnhub.py (before is_rate_limited)

def get_price_history(
    self,
    symbol: str,
    start: date,
    end: date,
    interval: str = "1d",
) -> list[PriceBar]:
    """Fetch daily OHLCV bars via ``/stock/candle``."""
    now = datetime.now(timezone.utc)

    start_ts = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).timestamp())

    data = self._get("stock/candle", params={
        "symbol": symbol.upper(),
        "resolution": "D",
        "from": start_ts,
        "to": end_ts,
    })

    if not data or data.get("s") == "no_data":
        return []

    closes = data.get("c", [])
    highs = data.get("h", [])
    lows = data.get("l", [])
    opens = data.get("o", [])
    timestamps = data.get("t", [])
    volumes = data.get("v", [])

    bars: list[PriceBar] = []
    for i in range(len(closes)):
        try:
            bar_date = datetime.fromtimestamp(timestamps[i], tz=timezone.utc).date()
            bars.append(PriceBar(
                symbol=symbol.upper(),
                date=bar_date,
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                adj_close=float(closes[i]),
                volume=int(volumes[i]),
                source=_SOURCE,
                fetched_at=now,
            ))
        except Exception as exc:
            logger.warning("Skipping Finnhub bar for %s: %s", symbol, exc)
            continue

    return bars
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetPriceHistory -v
```

Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add onefinance/providers/finnhub.py tests/unit/test_finnhub_provider.py
git commit -m "feat(m6): FinnhubProvider.get_price_history"
```

---

## Task 3: FinnhubProvider.get_quote

**Files:**
- Modify: `onefinance/providers/finnhub.py`
- Modify: `tests/unit/test_finnhub_provider.py`

Finnhub `/quote` response: `{"c": price, "h": high, "l": low, "o": open, "pc": prev_close, "t": unix_ts, "v": volume}`.

- [ ] **Step 1: Add tests**

```python
# Append to tests/unit/test_finnhub_provider.py

class TestGetQuote:
    _quote_data = {
        "c": 185.64,
        "d": 1.64,
        "dp": 0.89,
        "h": 186.00,
        "l": 184.50,
        "o": 184.80,
        "pc": 184.00,
        "t": 1704204000,
        "v": 52000000,
    }

    def test_returns_quote(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._quote_data)):
            q = provider.get_quote("AAPL")
        assert isinstance(q, Quote)
        assert q.symbol == "AAPL"
        assert q.price == 185.64
        assert q.source == "finnhub"

    def test_no_data_raises(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({"c": None})):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("AAPL")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"

    def test_symbol_uppercased(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._quote_data)):
            q = provider.get_quote("aapl")
        assert q.symbol == "AAPL"
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetQuote -v 2>&1 | head -10
```

- [ ] **Step 3: Implement get_quote**

```python
# Add to FinnhubProvider in onefinance/providers/finnhub.py

def get_quote(self, symbol: str) -> Quote:
    """Fetch realtime quote via ``/quote``."""
    now = datetime.now(timezone.utc)

    data = self._get("quote", params={"symbol": symbol.upper()})

    if not data or data.get("c") is None:
        raise ProviderError(
            code="SYMBOL_NOT_FOUND",
            message=f"No quote found for '{symbol}' via Finnhub",
            provider=self.name,
            retry_safe=False,
        )

    ts = data.get("t")
    timestamp = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else now

    return Quote(
        symbol=symbol.upper(),
        timestamp=timestamp,
        price=float(data["c"]),
        bid=None,
        ask=None,
        volume=int(data.get("v", 0) or 0),
        source=_SOURCE,
        fetched_at=now,
    )
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetQuote -v
```

Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add onefinance/providers/finnhub.py tests/unit/test_finnhub_provider.py
git commit -m "feat(m6): FinnhubProvider.get_quote"
```

---

## Task 4: FinnhubProvider.get_info

**Files:**
- Modify: `onefinance/providers/finnhub.py`
- Modify: `tests/unit/test_finnhub_provider.py`

Finnhub `/stock/profile2` response: `{"name": "Apple Inc", "exchange": "NASDAQ NMS - GLOBAL MARKET", "finnhubIndustry": "Technology", "country": "US", "currency": "USD", "marketCapitalization": 2900000.0, "weburl": "https://www.apple.com/", "ticker": "AAPL"}`. Note: `marketCapitalization` is in **millions USD**.

- [ ] **Step 1: Add tests**

```python
# Append to tests/unit/test_finnhub_provider.py

class TestGetInfo:
    _profile_data = {
        "name": "Apple Inc",
        "exchange": "NASDAQ NMS - GLOBAL MARKET",
        "finnhubIndustry": "Technology",
        "country": "US",
        "currency": "USD",
        "marketCapitalization": 2900000.0,
        "weburl": "https://www.apple.com/",
        "ticker": "AAPL",
    }

    def test_returns_company_info(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._profile_data)):
            info = provider.get_info("AAPL")
        assert isinstance(info, CompanyInfo)
        assert info.name == "Apple Inc"
        assert info.industry == "Technology"
        assert info.country == "US"
        assert info.currency == "USD"
        assert info.source == "finnhub"

    def test_market_cap_converted_from_millions(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._profile_data)):
            info = provider.get_info("AAPL")
        assert info.market_cap == 2_900_000.0 * 1_000_000

    def test_empty_response_raises(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({})):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_info("FAKE")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetInfo -v 2>&1 | head -10
```

- [ ] **Step 3: Implement get_info**

```python
# Add to FinnhubProvider in onefinance/providers/finnhub.py

def get_info(self, symbol: str) -> CompanyInfo:
    """Fetch company profile via ``/stock/profile2``."""
    now = datetime.now(timezone.utc)

    data = self._get("stock/profile2", params={"symbol": symbol.upper()})

    if not data or not data.get("name"):
        raise ProviderError(
            code="SYMBOL_NOT_FOUND",
            message=f"No profile found for '{symbol}' via Finnhub",
            provider=self.name,
            retry_safe=False,
        )

    raw_currency = data.get("currency")
    currency: str | None = None
    if raw_currency and isinstance(raw_currency, str) and len(raw_currency) == 3:
        currency = raw_currency.upper()

    market_cap_m = data.get("marketCapitalization")
    market_cap = float(market_cap_m) * 1_000_000 if market_cap_m is not None else None

    return CompanyInfo(
        symbol=symbol.upper(),
        name=data.get("name") or symbol,
        exchange=data.get("exchange"),
        sector=None,
        industry=data.get("finnhubIndustry"),
        country=data.get("country"),
        market_cap=market_cap,
        description=None,
        website=data.get("weburl"),
        employees=None,
        currency=currency,
        source=_SOURCE,
        fetched_at=now,
    )
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetInfo -v
```

Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add onefinance/providers/finnhub.py tests/unit/test_finnhub_provider.py
git commit -m "feat(m6): FinnhubProvider.get_info"
```

---

## Task 5: FinnhubProvider.get_financials

**Files:**
- Modify: `onefinance/providers/finnhub.py`
- Modify: `tests/unit/test_finnhub_provider.py`

Finnhub `/financials-reported` returns XBRL as-reported data. Each entry has `report.ic / .bs / .cf` arrays of `{"concept": "us-gaap:Revenues", "label": "...", "unit": "USD", "value": 383285000000}`. Add helper `_xbrl_float(vals, concepts)` at module level to look up concepts by name.

- [ ] **Step 1: Add tests**

```python
# Append to tests/unit/test_finnhub_provider.py

class TestGetFinancials:
    _income_data = {
        "data": [{
            "endDate": "2023-09-30",
            "year": 2023,
            "quarter": 0,
            "report": {
                "ic": [
                    {"concept": "us-gaap:Revenues", "label": "Revenue", "unit": "USD", "value": 383285000000},
                    {"concept": "us-gaap:CostOfRevenue", "label": "Cost of Revenue", "unit": "USD", "value": 214137000000},
                    {"concept": "us-gaap:GrossProfit", "label": "Gross Profit", "unit": "USD", "value": 169148000000},
                    {"concept": "us-gaap:OperatingIncomeLoss", "label": "Operating Income", "unit": "USD", "value": 114301000000},
                    {"concept": "us-gaap:NetIncomeLoss", "label": "Net Income", "unit": "USD", "value": 96995000000},
                    {"concept": "us-gaap:EarningsPerShareBasic", "label": "EPS Basic", "unit": "USD/shares", "value": 6.16},
                    {"concept": "us-gaap:EarningsPerShareDiluted", "label": "EPS Diluted", "unit": "USD/shares", "value": 6.13},
                ],
                "bs": [],
                "cf": [],
            }
        }]
    }

    def test_income_statement(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._income_data)):
            results = provider.get_financials("AAPL", "income", "annual")
        assert len(results) == 1
        stmt = results[0]
        assert isinstance(stmt, IncomeStatement)
        assert stmt.revenue == 383285000000
        assert stmt.net_income == 96995000000
        assert stmt.period == "2023-FY"
        assert stmt.source == "finnhub"

    def test_invalid_statement_raises(self, provider):
        with pytest.raises(ProviderError) as exc_info:
            provider.get_financials("AAPL", "invalid", "annual")
        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_empty_data_returns_empty_list(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({"data": []})):
            results = provider.get_financials("AAPL", "income", "annual")
        assert results == []
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetFinancials -v 2>&1 | head -10
```

- [ ] **Step 3: Implement get_financials + helper**

```python
# Add at module level in onefinance/providers/finnhub.py (before the class)

def _xbrl_float(vals: dict[str, Any], concepts: list[str]) -> float:
    """Extract first matching XBRL concept value as float, default 0.0."""
    for concept in concepts:
        v = vals.get(concept)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                continue
    return 0.0


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
```

```python
# Add to FinnhubProvider in onefinance/providers/finnhub.py

def get_financials(
    self,
    symbol: str,
    statement: str,
    period: str,
) -> list[IncomeStatement | BalanceSheet | CashFlow]:
    """Fetch as-reported financials via ``/financials-reported``."""
    now = datetime.now(timezone.utc)

    stmt_map = {"income": "ic", "balance": "bs", "cashflow": "cf"}
    stmt_code = stmt_map.get(statement)
    if stmt_code is None:
        raise ProviderError(
            code="INVALID_ARGUMENT",
            message=f"Unknown statement type: '{statement}'. Use 'income', 'balance', or 'cashflow'.",
            provider=self.name,
            retry_safe=False,
        )

    freq = "quarterly" if period == "quarterly" else "annual"
    data = self._get("financials-reported", params={
        "symbol": symbol.upper(),
        "statement": stmt_code,
        "freq": freq,
    })

    entries = data.get("data", []) if isinstance(data, dict) else []
    if not entries:
        return []

    results: list[IncomeStatement | BalanceSheet | CashFlow] = []
    for entry in entries[:5]:
        report = entry.get("report", {})
        concepts = report.get(stmt_code, [])
        vals: dict[str, Any] = {c.get("concept", ""): c.get("value") for c in concepts}

        end_date_str = entry.get("endDate", "")
        try:
            fiscal_date = date.fromisoformat(end_date_str)
        except (ValueError, TypeError):
            continue

        year = entry.get("year", fiscal_date.year)
        q = entry.get("quarter", 0)
        period_str = f"{year}-Q{q}" if q else f"{year}-FY"

        try:
            if statement == "income":
                results.append(IncomeStatement(
                    symbol=symbol.upper(),
                    period=period_str,
                    fiscal_date=fiscal_date,
                    revenue=_xbrl_float(vals, ["us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"]),
                    cost_of_revenue=_xbrl_float(vals, ["us-gaap:CostOfRevenue", "us-gaap:CostOfGoodsAndServicesSold"]),
                    gross_profit=_xbrl_float(vals, ["us-gaap:GrossProfit"]),
                    operating_income=_xbrl_float(vals, ["us-gaap:OperatingIncomeLoss"]),
                    net_income=_xbrl_float(vals, ["us-gaap:NetIncomeLoss"]),
                    eps_basic=_xbrl_float(vals, ["us-gaap:EarningsPerShareBasic"]),
                    eps_diluted=_xbrl_float(vals, ["us-gaap:EarningsPerShareDiluted"]),
                    currency="USD",
                    source=_SOURCE,
                    fetched_at=now,
                ))
            elif statement == "balance":
                results.append(BalanceSheet(
                    symbol=symbol.upper(),
                    period=period_str,
                    fiscal_date=fiscal_date,
                    total_assets=_xbrl_float(vals, ["us-gaap:Assets"]),
                    total_liabilities=_xbrl_float(vals, ["us-gaap:Liabilities"]),
                    total_equity=_xbrl_float(vals, [
                        "us-gaap:StockholdersEquity",
                        "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                    ]),
                    cash_and_equivalents=_xbrl_float(vals, ["us-gaap:CashAndCashEquivalentsAtCarryingValue"]),
                    total_debt=_xbrl_float(vals, ["us-gaap:LongTermDebt", "us-gaap:LongTermDebtNoncurrent"]),
                    currency="USD",
                    source=_SOURCE,
                    fetched_at=now,
                ))
            else:
                op_cf = _xbrl_float(vals, ["us-gaap:NetCashProvidedByUsedInOperatingActivities"])
                capex = _xbrl_float(vals, ["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"])
                results.append(CashFlow(
                    symbol=symbol.upper(),
                    period=period_str,
                    fiscal_date=fiscal_date,
                    operating_cash_flow=op_cf,
                    capital_expenditure=capex,
                    free_cash_flow=op_cf - capex,
                    dividends_paid=_xbrl_float(vals, ["us-gaap:PaymentsOfDividends"]),
                    currency="USD",
                    source=_SOURCE,
                    fetched_at=now,
                ))
        except Exception as exc:
            logger.warning("Skipping Finnhub financial entry for %s: %s", symbol, exc)
            continue

    return results
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetFinancials -v
```

Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add onefinance/providers/finnhub.py tests/unit/test_finnhub_provider.py
git commit -m "feat(m6): FinnhubProvider.get_financials with XBRL normalization"
```

---

## Task 6: FinnhubProvider.get_ratios

**Files:**
- Modify: `onefinance/providers/finnhub.py`
- Modify: `tests/unit/test_finnhub_provider.py`

Finnhub `/stock/metric?metric=all` returns a single snapshot (not historical). Response: `{"metric": {"peAnnual": 29.5, "pbAnnual": 47.3, "psTTM": 7.8, "roeTTM": 1.56, "roaRfy": 0.28, "grossMarginTTM": 0.443, "operatingMarginTTM": 0.298, "netProfitMarginTTM": 0.253, "dividendYieldIndicatedAnnual": 0.0051, "currentRatioAnnual": 0.99, "totalDebt/totalEquityAnnual": 1.79}}`.

- [ ] **Step 1: Add tests**

```python
# Append to tests/unit/test_finnhub_provider.py

class TestGetRatios:
    _metric_data = {
        "metric": {
            "peAnnual": 29.5,
            "pbAnnual": 47.3,
            "psTTM": 7.8,
            "roeTTM": 1.56,
            "roaRfy": 0.28,
            "grossMarginTTM": 0.443,
            "operatingMarginTTM": 0.298,
            "netProfitMarginTTM": 0.253,
            "dividendYieldIndicatedAnnual": 0.0051,
            "currentRatioAnnual": 0.99,
            "totalDebt/totalEquityAnnual": 1.79,
        }
    }

    def test_returns_ratios(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._metric_data)):
            results = provider.get_ratios("AAPL", "annual")
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, FinancialRatios)
        assert r.pe_ratio == 29.5
        assert r.pb_ratio == 47.3
        assert r.source == "finnhub"

    def test_empty_metric_returns_empty(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({"metric": {}})):
            results = provider.get_ratios("AAPL", "annual")
        assert results == []
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetRatios -v 2>&1 | head -10
```

- [ ] **Step 3: Implement get_ratios**

```python
# Add to FinnhubProvider in onefinance/providers/finnhub.py

def get_ratios(self, symbol: str, period: str) -> list[FinancialRatios]:
    """Fetch current financial metrics via ``/stock/metric``."""
    now = datetime.now(timezone.utc)

    data = self._get("stock/metric", params={"symbol": symbol.upper(), "metric": "all"})
    metric = data.get("metric", {}) if isinstance(data, dict) else {}
    if not metric:
        return []

    return [FinancialRatios(
        symbol=symbol.upper(),
        period="current",
        fiscal_date=date.today(),
        pe_ratio=_safe_float(metric.get("peAnnual") or metric.get("peTTM")),
        pb_ratio=_safe_float(metric.get("pbAnnual") or metric.get("pbQuarterly")),
        ps_ratio=_safe_float(metric.get("psAnnual") or metric.get("psTTM")),
        debt_to_equity=_safe_float(metric.get("totalDebt/totalEquityAnnual")),
        current_ratio=_safe_float(metric.get("currentRatioAnnual")),
        return_on_equity=_safe_float(metric.get("roeTTM")),
        return_on_assets=_safe_float(metric.get("roaRfy")),
        gross_margin=_safe_float(metric.get("grossMarginAnnual") or metric.get("grossMarginTTM")),
        operating_margin=_safe_float(metric.get("operatingMarginAnnual") or metric.get("operatingMarginTTM")),
        net_margin=_safe_float(metric.get("netProfitMarginAnnual") or metric.get("netProfitMarginTTM")),
        dividend_yield=_safe_float(metric.get("dividendYieldIndicatedAnnual")),
        source=_SOURCE,
        fetched_at=now,
    )]
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetRatios -v
```

Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add onefinance/providers/finnhub.py tests/unit/test_finnhub_provider.py
git commit -m "feat(m6): FinnhubProvider.get_ratios"
```

---

## Task 7: FinnhubProvider.get_earnings

**Files:**
- Modify: `onefinance/providers/finnhub.py`
- Modify: `tests/unit/test_finnhub_provider.py`

Finnhub `/stock/earnings` response: array of `{"actual": 2.18, "estimate": 2.10, "period": "2024-03-31", "quarter": 2, "surprise": 0.08, "surprisePercent": 3.81, "year": 2024}`.

- [ ] **Step 1: Add tests**

```python
# Append to tests/unit/test_finnhub_provider.py

class TestGetEarnings:
    _earnings_data = [
        {"actual": 2.18, "estimate": 2.10, "period": "2024-03-31",
         "quarter": 2, "surprise": 0.08, "surprisePercent": 3.81, "year": 2024},
        {"actual": 2.46, "estimate": 2.35, "period": "2023-12-31",
         "quarter": 1, "surprise": 0.11, "surprisePercent": 4.68, "year": 2024},
    ]

    def test_returns_earnings(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._earnings_data)):
            results = provider.get_earnings("AAPL")
        assert len(results) == 2
        assert all(isinstance(r, EarningsRecord) for r in results)
        assert results[0].eps_actual == 2.18
        assert results[0].eps_estimate == 2.10
        assert results[0].eps_surprise == 0.08
        assert results[0].period == "2024-Q2"
        assert results[0].source == "finnhub"

    def test_empty_response_returns_empty(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response([])):
            results = provider.get_earnings("AAPL")
        assert results == []

    def test_revenue_fields_are_none(self, provider):
        # Finnhub earnings endpoint does not include revenue actuals/estimates
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._earnings_data)):
            results = provider.get_earnings("AAPL")
        assert results[0].revenue_actual is None
        assert results[0].revenue_estimate is None
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetEarnings -v 2>&1 | head -10
```

- [ ] **Step 3: Implement get_earnings**

```python
# Add to FinnhubProvider in onefinance/providers/finnhub.py

def get_earnings(self, symbol: str) -> list[EarningsRecord]:
    """Fetch earnings surprises via ``/stock/earnings``."""
    now = datetime.now(timezone.utc)

    data = self._get("stock/earnings", params={"symbol": symbol.upper(), "limit": 8})

    if not data or not isinstance(data, list):
        return []

    results: list[EarningsRecord] = []
    for item in data:
        period_str = item.get("period")
        if not period_str:
            continue
        try:
            fiscal_date = date.fromisoformat(period_str)
        except (ValueError, TypeError):
            continue

        year = item.get("year", fiscal_date.year)
        q = item.get("quarter", 0)
        period_label = f"{year}-Q{q}" if q else f"{year}-FY"

        results.append(EarningsRecord(
            symbol=symbol.upper(),
            period=period_label,
            fiscal_date=fiscal_date,
            eps_actual=_safe_float(item.get("actual")),
            eps_estimate=_safe_float(item.get("estimate")),
            eps_surprise=_safe_float(item.get("surprise")),
            revenue_actual=None,
            revenue_estimate=None,
            source=_SOURCE,
            fetched_at=now,
        ))

    return results
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetEarnings -v
```

Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add onefinance/providers/finnhub.py tests/unit/test_finnhub_provider.py
git commit -m "feat(m6): FinnhubProvider.get_earnings"
```

---

## Task 8: FinnhubProvider.get_insider_trades

**Files:**
- Modify: `onefinance/providers/finnhub.py`
- Modify: `tests/unit/test_finnhub_provider.py`

Finnhub `/stock/insider-transactions` response: `{"data": [{"change": -50000, "filingDate": "2024-01-15", "name": "Tim Cook", "share": 3000000, "source": "A", "symbol": "AAPL", "transactionDate": "2024-01-12", "transactionPrice": 185.64, "transactionCode": "S"}]}`. Transaction codes: `P` = buy, `S`/`F` = sell, `M`/`A` = exercise.

- [ ] **Step 1: Add tests**

```python
# Append to tests/unit/test_finnhub_provider.py

class TestGetInsiderTrades:
    _trades_data = {
        "data": [
            {
                "change": -50000,
                "filingDate": "2024-01-15",
                "name": "Tim Cook",
                "share": 3000000,
                "source": "A",
                "symbol": "AAPL",
                "transactionDate": "2024-01-12",
                "transactionPrice": 185.64,
                "transactionCode": "S",
            },
            {
                "change": 10000,
                "filingDate": "2024-01-10",
                "name": "Luca Maestri",
                "share": 500000,
                "source": "O",
                "symbol": "AAPL",
                "transactionDate": "2024-01-09",
                "transactionPrice": 183.00,
                "transactionCode": "P",
            },
        ]
    }

    def test_returns_trades(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._trades_data)):
            results = provider.get_insider_trades("AAPL")
        assert len(results) == 2
        assert all(isinstance(r, InsiderTrade) for r in results)
        assert results[0].insider_name == "Tim Cook"
        assert results[0].trade_type == "sell"
        assert results[1].trade_type == "buy"
        assert results[0].source == "finnhub"

    def test_since_filter(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._trades_data)):
            results = provider.get_insider_trades("AAPL", since=date(2024, 1, 12))
        assert len(results) == 1
        assert results[0].insider_name == "Tim Cook"

    def test_empty_data_returns_empty(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({"data": []})):
            results = provider.get_insider_trades("AAPL")
        assert results == []
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_finnhub_provider.py::TestGetInsiderTrades -v 2>&1 | head -10
```

- [ ] **Step 3: Implement get_insider_trades**

```python
# Add to FinnhubProvider in onefinance/providers/finnhub.py

def get_insider_trades(
    self,
    symbol: str,
    since: date | None = None,
) -> list[InsiderTrade]:
    """Fetch insider transactions via ``/stock/insider-transactions``."""
    now = datetime.now(timezone.utc)

    data = self._get("stock/insider-transactions", params={"symbol": symbol.upper()})

    entries = data.get("data", []) if isinstance(data, dict) else []
    if not entries:
        return []

    results: list[InsiderTrade] = []
    for item in entries:
        filing_date_str = item.get("filingDate")
        if not filing_date_str:
            continue
        try:
            filing_d = date.fromisoformat(filing_date_str[:10])
        except (ValueError, TypeError):
            continue

        if since and filing_d < since:
            continue

        trade_date_str = item.get("transactionDate")
        trade_d: date | None = None
        if trade_date_str:
            try:
                trade_d = date.fromisoformat(trade_date_str[:10])
            except (ValueError, TypeError):
                pass

        code = (item.get("transactionCode") or "").upper()
        if code == "P":
            trade_type = "buy"
        elif code in ("S", "F"):
            trade_type = "sell"
        elif code in ("M", "A"):
            trade_type = "exercise"
        else:
            trade_type = code.lower() or "unknown"

        shares = abs(float(item.get("change", 0) or 0))
        price = _safe_float(item.get("transactionPrice"))
        total_value = (shares * price) if price is not None else None

        results.append(InsiderTrade(
            symbol=symbol.upper(),
            filing_date=filing_d,
            trade_date=trade_d,
            insider_name=item.get("name", "Unknown"),
            insider_title=item.get("source"),
            trade_type=trade_type,
            shares=shares,
            price_per_share=price,
            total_value=total_value,
            shares_owned_after=_safe_float(item.get("share")),
            source=_SOURCE,
            fetched_at=now,
        ))

    return results
```

- [ ] **Step 4: Run all Finnhub tests**

```bash
python -m pytest tests/unit/test_finnhub_provider.py -v
```

Expected: all tests PASS (26+)

- [ ] **Step 5: Run full suite to check regressions**

```bash
python -m pytest tests/unit/ -v --tb=short 2>&1 | tail -20
```

Expected: all prior tests still PASS

- [ ] **Step 6: Commit**

```bash
git add onefinance/providers/finnhub.py tests/unit/test_finnhub_provider.py
git commit -m "feat(m6): FinnhubProvider.get_insider_trades — M6 complete"
```

---

## Task 9: TwelveDataProvider skeleton + get_price_history

**Files:**
- Create: `onefinance/providers/twelve_data.py`
- Create: `tests/unit/test_twelve_data_provider.py`

Twelve Data `/time_series` response: `{"meta": {...}, "values": [{"datetime": "2024-01-02", "open": "184.22", "high": "185.88", "low": "183.43", "close": "184.25", "volume": "58414500"}], "status": "ok"}`. All values are strings. Rate-limit signal: HTTP 429 OR body `{"code": 429, "message": "..."}`.

- [ ] **Step 1: Write tests**

```python
# tests/unit/test_twelve_data_provider.py
"""Unit tests for TwelveDataProvider with mocked httpx responses."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from onefinance.core.errors import ConfigError, ProviderError, RateLimitError
from onefinance.core.models import PriceBar, Quote
from onefinance.providers.twelve_data import TwelveDataProvider


@pytest.fixture
def provider() -> TwelveDataProvider:
    return TwelveDataProvider(api_key="test_key", timeout=5)


def _mock_response(data: Any, status_code: int = 200) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data) if not isinstance(data, str) else data
    return resp


class TestConstructor:
    def test_api_key_from_param(self):
        p = TwelveDataProvider(api_key="my_key")
        assert p._api_key == "my_key"

    def test_api_key_from_env(self):
        with patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "env_key"}):
            p = TwelveDataProvider()
            assert p._api_key == "env_key"

    def test_missing_key_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ConfigError):
                TwelveDataProvider()

    def test_name(self):
        assert TwelveDataProvider(api_key="k").name == "twelve_data"


class TestRateLimitDetection:
    def test_http_429_raises(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({}, 429)):
            with pytest.raises(RateLimitError):
                provider._get("time_series", {})

    def test_body_429_code_raises(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({"code": 429, "message": "Too Many Requests"})):
            with pytest.raises(RateLimitError):
                provider._get("time_series", {})


class TestGetPriceHistory:
    _ts_data = {
        "meta": {"symbol": "AAPL", "interval": "1day"},
        "values": [
            {"datetime": "2024-01-03", "open": "184.22", "high": "185.88",
             "low": "183.43", "close": "184.25", "volume": "58414500"},
            {"datetime": "2024-01-02", "open": "185.00", "high": "186.10",
             "low": "184.50", "close": "185.64", "volume": "52000000"},
        ],
        "status": "ok",
    }

    def test_returns_bars_in_asc_order(self, provider):
        data = dict(self._ts_data)
        data["values"] = list(reversed(self._ts_data["values"]))  # API may return desc
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._ts_data)):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))
        assert len(bars) == 2
        assert all(isinstance(b, PriceBar) for b in bars)
        assert bars[0].close == 184.25
        assert bars[0].source == "twelve_data"

    def test_empty_values_returns_empty(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({"values": [], "status": "ok"})):
            bars = provider.get_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 3))
        assert bars == []

    def test_symbol_uppercased(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._ts_data)):
            bars = provider.get_price_history("aapl", date(2024, 1, 2), date(2024, 1, 3))
        assert bars[0].symbol == "AAPL"
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_twelve_data_provider.py -v 2>&1 | head -15
```

Expected: `ModuleNotFoundError: No module named 'onefinance.providers.twelve_data'`

- [ ] **Step 3: Create TwelveDataProvider**

```python
# onefinance/providers/twelve_data.py
"""Twelve Data provider adapter.

Uses the Twelve Data REST API (https://api.twelvedata.com).
Requires an API key set via the ``TWELVE_DATA_API_KEY`` environment variable.

Free tier: 800 calls/day, 8 calls/minute.
Supports: get_price_history, get_quote only (see design doc §3 capability matrix).

See design doc §7 for rate-limit detection details.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import httpx

from onefinance.core.errors import ConfigError, ProviderError, RateLimitError
from onefinance.core.models import PriceBar, Quote
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "twelve_data"
_BASE_URL = "https://api.twelvedata.com"


class TwelveDataProvider(BaseProvider):
    """Provider adapter for Twelve Data (price history and quotes only).

    Parameters
    ----------
    api_key:
        Twelve Data API key. If ``None``, reads from ``TWELVE_DATA_API_KEY`` env var.
    timeout:
        HTTP request timeout in seconds.
    base_url:
        Override the base URL (useful for testing).
    """

    name = _SOURCE

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 10,
        base_url: str = _BASE_URL,
    ) -> None:
        self._api_key = api_key or os.environ.get("TWELVE_DATA_API_KEY")
        if not self._api_key:
            raise ConfigError(
                "TWELVE_DATA_API_KEY not set. Set it in your environment or pass api_key="
            )
        self._timeout = timeout
        self._base_url = base_url
        self._client = httpx.Client(timeout=timeout)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Authenticated GET to Twelve Data API.

        Raises ``RateLimitError`` on HTTP 429 or body code 429,
        ``ProviderError`` on other failures.
        """
        url = f"{self._base_url}/{path}"
        req_params = dict(params or {})
        req_params["apikey"] = self._api_key

        try:
            resp = self._client.get(url, params=req_params)
        except httpx.HTTPError as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"Twelve Data request failed: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if resp.status_code == 429:
            raise RateLimitError(
                provider=self.name,
                message="Twelve Data rate limit hit (HTTP 429)",
                retry_after_seconds=60,
            )

        if resp.status_code != 200:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"Twelve Data HTTP {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                retry_safe=resp.status_code >= 500,
            )

        data = resp.json()

        if isinstance(data, dict) and data.get("code") == 429:
            raise RateLimitError(
                provider=self.name,
                message=f"Twelve Data quota hit: {data.get('message', '')}",
                retry_after_seconds=60,
            )

        if isinstance(data, dict) and data.get("status") == "error":
            raise ProviderError(
                code="PROVIDER_ERROR",
                message=f"Twelve Data error: {data.get('message', '')}",
                provider=self.name,
                retry_safe=False,
            )

        return data

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[PriceBar]:
        """Fetch daily OHLCV bars via ``/time_series``."""
        now = datetime.now(timezone.utc)

        data = self._get("time_series", params={
            "symbol": symbol.upper(),
            "interval": "1day",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "outputsize": 5000,
            "order": "ASC",
        })

        values = data.get("values", []) if isinstance(data, dict) else []
        if not values:
            return []

        bars: list[PriceBar] = []
        for item in values:
            try:
                bars.append(PriceBar(
                    symbol=symbol.upper(),
                    date=date.fromisoformat(item["datetime"][:10]),
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    adj_close=float(item.get("adjusted_close", item["close"])),
                    volume=int(float(item.get("volume", 0))),
                    source=_SOURCE,
                    fetched_at=now,
                ))
            except Exception as exc:
                logger.warning("Skipping Twelve Data bar for %s: %s", symbol, exc)
                continue

        return bars

    def is_rate_limited(self, response: Any) -> bool:
        if isinstance(response, httpx.Response):
            if response.status_code == 429:
                return True
            try:
                return response.json().get("code") == 429
            except Exception:
                return False
        if isinstance(response, Exception):
            return "429" in str(response)
        return False

    def cooldown_for(self, response: Any) -> float:
        return 60.0


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/unit/test_twelve_data_provider.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add onefinance/providers/twelve_data.py tests/unit/test_twelve_data_provider.py
git commit -m "feat(m7): TwelveDataProvider skeleton + get_price_history"
```

---

## Task 10: TwelveDataProvider.get_quote

**Files:**
- Modify: `onefinance/providers/twelve_data.py`
- Modify: `tests/unit/test_twelve_data_provider.py`

Twelve Data `/quote` response: `{"symbol": "AAPL", "close": "185.64", "volume": "52000000", "timestamp": 1704204000, "datetime": "2024-01-02 16:00:00", ...}`.

- [ ] **Step 1: Add tests**

```python
# Append to tests/unit/test_twelve_data_provider.py

class TestGetQuote:
    _quote_data = {
        "symbol": "AAPL",
        "name": "Apple Inc",
        "exchange": "NASDAQ",
        "currency": "USD",
        "datetime": "2024-01-02",
        "timestamp": 1704204000,
        "open": "184.22",
        "high": "186.00",
        "low": "184.00",
        "close": "185.64",
        "volume": "52000000",
        "previous_close": "184.00",
        "change": "1.64",
        "percent_change": "0.89",
        "is_market_open": False,
    }

    def test_returns_quote(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._quote_data)):
            q = provider.get_quote("AAPL")
        assert isinstance(q, Quote)
        assert q.symbol == "AAPL"
        assert q.price == 185.64
        assert q.volume == 52000000
        assert q.source == "twelve_data"

    def test_symbol_uppercased(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response(self._quote_data)):
            q = provider.get_quote("aapl")
        assert q.symbol == "AAPL"

    def test_error_status_raises(self, provider):
        with patch.object(provider._client, "get",
                          return_value=_mock_response({"status": "error", "message": "Symbol not found"})):
            with pytest.raises(ProviderError) as exc_info:
                provider.get_quote("FAKE")
        assert exc_info.value.code == "SYMBOL_NOT_FOUND"
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/unit/test_twelve_data_provider.py::TestGetQuote -v 2>&1 | head -10
```

- [ ] **Step 3: Implement get_quote**

```python
# Add to TwelveDataProvider in onefinance/providers/twelve_data.py

def get_quote(self, symbol: str) -> Quote:
    """Fetch current quote via ``/quote``."""
    now = datetime.now(timezone.utc)

    data = self._get("quote", params={"symbol": symbol.upper()})

    if not data or not isinstance(data, dict) or not data.get("close"):
        raise ProviderError(
            code="SYMBOL_NOT_FOUND",
            message=f"No quote found for '{symbol}' via Twelve Data",
            provider=self.name,
            retry_safe=False,
        )

    ts = data.get("timestamp")
    try:
        timestamp = datetime.fromtimestamp(float(ts), tz=timezone.utc) if ts else now
    except (ValueError, TypeError):
        timestamp = now

    return Quote(
        symbol=symbol.upper(),
        timestamp=timestamp,
        price=float(data["close"]),
        bid=None,
        ask=None,
        volume=int(float(data.get("volume", 0))),
        source=_SOURCE,
        fetched_at=now,
    )
```

- [ ] **Step 4: Run all M7 tests**

```bash
python -m pytest tests/unit/test_twelve_data_provider.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Run full unit suite**

```bash
python -m pytest tests/unit/ --tb=short 2>&1 | tail -10
```

Expected: 200+ tests, all PASS

- [ ] **Step 6: Final commit**

```bash
git add onefinance/providers/twelve_data.py tests/unit/test_twelve_data_provider.py
git commit -m "feat(m7): TwelveDataProvider.get_quote — M7 complete"
```
