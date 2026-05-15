# Gap Analysis: invest/ Scripts vs one finance data

> [!NOTE]
> **Scope: US equities only.** A-share (China) and HK market support are explicitly out of scope.

## Scripts Inventoried

| Script | Purpose | Data Sources Used |
|---|---|---|
| `fetch_stock_data.py` | OHLCV + technical indicators + live quote | yfinance, FMP, akshare |
| `fetch_fundamentals.py` | Full fundamental analysis (profile, financials, ratios, DCF) | FMP (via `FMPClient`) |
| `fetch_entry_data.py` | Entry-signal data (FMP-first, yfinance fallback) | FMP, yfinance |
| `korea_customs_tracker.py` | Korea customs semiconductor export tracking | Web scraping (no market data) |
| `disk_cache.py` | JSON file-based caching utility | N/A (infrastructure) |

> [!NOTE]
> `korea_customs_tracker.py` is a macro/trade-intelligence tool scraping KCS data. It doesn't use market data APIs and is **out of scope** for one finance data.

---

## Data Needs Extracted from Scripts

### fetch_stock_data.py

| Need | How It's Done Today | one finance data Equivalent |
|---|---|---|
| Daily OHLCV bars (US) | yfinance or FMP `historical-price-eod/full` | ✅ `get_price_history()` |
| Daily OHLCV bars (A-shares) | akshare `stock_zh_a_hist` | ❌ **Gap** |
| Daily OHLCV bars (HK) | yfinance (`.HK` suffix) | ⚠️ Partial — yfinance works but no HK ticker normalization |
| Live quote (US) | yfinance 1m bars or FMP `/quote` | ✅ `get_quote()` |
| Live quote (A-shares) | akshare `stock_zh_a_spot_em` | ❌ **Gap** |
| Live quote (HK) | yfinance 1m bars | ⚠️ Partial |
| Company name (US) | yfinance `.info` or FMP `/profile` | ✅ `get_info()` → `CompanyInfo.name` |
| Company name (A-shares) | akshare `stock_info_a_code_name` | ❌ **Gap** |
| Technical indicators (MA, MACD, RSI, ATR) | Computed from OHLCV in-script | ❌ **Gap** — not in scope (see note) |
| Ticker normalization (US/HK/A-share) | `_classify_code()` / `_to_yfinance_ticker()` | ❌ **Gap** |

### fetch_fundamentals.py

| Need | How It's Done Today | one finance data Equivalent |
|---|---|---|
| Company profile | FMP `/profile` | ✅ `get_info()` |
| Live quote | FMP `/quote` | ✅ `get_quote()` |
| Income statement (annual/quarterly) | FMP `/income-statement` | ✅ `get_financials(statement="income")` |
| Cash flow (annual/quarterly) | FMP `/cash-flow-statement` | ✅ `get_financials(statement="cashflow")` |
| Balance sheet (annual/quarterly) | FMP `/balance-sheet-statement` | ✅ `get_financials(statement="balance")` |
| Key metrics (marketCap, EV, PE, PB, ROE, ROIC) | FMP `/key-metrics` | ⚠️ **Partial** — `FinancialRatios` has PE, PB, ROE, but missing EV, ROIC, freeCashFlowYield, bookValuePerShare |
| Financial ratios (margins, debt/equity, current) | FMP `/ratios` | ✅ `get_ratios()` |
| DCF valuation | FMP `/discounted-cash-flow` | ❌ **Gap** |

### fetch_entry_data.py

Same as `fetch_stock_data.py` + `fetch_fundamentals.py` combined — no new data needs.

---

## Gap Summary

### ~~Gap 1: No A-share / China Market Support~~ — OUT OF SCOPE

### ~~Gap 2: No Ticker Normalization~~ — OUT OF SCOPE (US-only; plain tickers like `AAPL` work natively)

### Gap 3: Missing Key Metrics Fields ✅ DONE
**Closed.** Added `enterprise_value`, `roic`, `ev_to_ebitda`, `ev_to_sales`, `free_cash_flow_yield`, `book_value_per_share`, `revenue_per_share`, `quick_ratio`, `interest_coverage` to `FinancialRatios`.

### Gap 4: No DCF Valuation Endpoint ✅ DONE
**Closed.** Added `DCFValuation` model, `FMPProvider.get_dcf()`, `OneFinanceClient.get_dcf()`, CLI `ofclient dcf`.

### Gap 5: Technical Indicator Computation ✅ DONE
**Closed.** Added `onefinance.indicators` module with `compute_indicators(bars) → TechnicalIndicators`. Pure Python, no pandas/numpy. Covers MA(5/10/20/60), MACD(12,26,9), RSI(14), ATR(14), volume ratio, MA alignment, 5-level trend status, bias, support/resistance.

### Gap 6: Missing `IncomeStatement` Fields ✅ DONE
**Closed.** Added `ebitda`, `research_and_development`, `sga_expenses`.

### Gap 7: Missing `CashFlow` Fields ✅ DONE
**Closed.** Added `depreciation_and_amortization`, `change_in_working_capital`, `stock_based_compensation`, `common_stock_repurchased`.

### Gap 8: Missing `BalanceSheet` Fields ✅ DONE
**Closed.** Added `short_term_investments`, `total_current_assets`, `total_current_liabilities`, `net_debt`, `goodwill`, `inventory`.

---

## Plan Status

### Phase 1: Enrich Existing Models ✅ COMPLETE
All optional fields added to `IncomeStatement`, `BalanceSheet`, `CashFlow`, `FinancialRatios`. FMP and Finnhub adapters updated.

### Phase 2: DCF Endpoint ✅ COMPLETE
`DCFValuation` model, `FMPProvider.get_dcf()`, `OneFinanceClient.get_dcf()`, tier table entry, cache TTL (7 days).

### ~~Phase 3: Ticker Normalization~~ — DROPPED (US-only scope)

### ~~Phase 4: AkshareProvider for China Markets~~ — DROPPED (US-only scope)

### Phase 5: Technical Indicators ✅ COMPLETE
`onefinance.indicators.compute_indicators()` — pure Python engine operating on `list[PriceBar]`. 28 tests.

---

## What Can Already Be Replaced Today

Even without closing any gaps, these invest/ script use cases are already covered:

| Use Case | Script Today | one finance data Today |
|---|---|---|
| US daily OHLCV | `fetch_stock_data.py` (800 lines) | `client.get_price_history("AAPL", start=..., end=...)` |
| US live quote | `fetch_stock_data.py` | `client.get_quote("AAPL")` |
| Company profile | `fetch_fundamentals.py` + `fetch_stock_data.py` | `client.get_info("AAPL")` |
| Income/balance/cashflow | `fetch_fundamentals.py` | `client.get_financials("AAPL", ...)` |
| Financial ratios | `fetch_fundamentals.py` | `client.get_ratios("AAPL", ...)` |
| Insider trades | N/A in scripts | `client.get_insider_trades("AAPL")` |
| Earnings | N/A in scripts | `client.get_earnings("AAPL")` |
| Multi-provider fallback | Hand-rolled in each script | Built into router |
| Disk caching | `disk_cache.py` (94 lines, JSON files) | `CacheManager` (SQLite, LRU, TTL-aware) |
