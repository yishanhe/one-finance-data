# ofclient Feedback & Feature Requests

## Existing Issues

- [ ] **OI data missing for high-priced stocks (MU, etc.)** — yfinance OI returns 0 for strikes above ~$995 on MU. Affects options chain analysis reliability. Workaround: Tradier as lead options provider (key required). (2026-06-17)
- [x] **`--no-cache` on quote is unnecessary** — quote already has 30s TTL, `--no-cache` just wastes API quota. Fixed in reporter skills. (2026-06-19)
- [x] **ofclient news returns empty headlines** — yfinance provider now skips articles with empty titles; if all raw items had empty titles it raises `ProviderError` to fall through to FMP/Finnhub. (2026-06-24)
- [x] **ofclient analyst for INTC returns empty** — Root cause: FMP `get_analyst_data`, `get_corporate_actions`, and `get_institutional_holders` were passing full URLs to `_get()` which prepended the base URL a second time, producing malformed double-URL requests. Fixed by passing only the path suffix. (2026-06-24)
- [x] **`financials --period quarterly` no `--limit`** — Added `--limit` / `-n` to `ofclient financials`. Returns most-recent N periods. (2026-06-24)
- [ ] **KR/JP price discrepancy** — 000660.KS and 285A.T fall back to yfinance. Adjusted closes differ materially from quote (Kioxia 10%+ gaps).
- [x] **ofclient estimates opaque period labels** — yfinance now maps `0q`→`current_Q`, `+1q`→`next_Q`, `0y`→`current_FY`, `+1y`→`next_FY`, etc. FMP already emits proper `fiscal_date` + `YYYY-FY` labels. (2026-06-24)
- [ ] **Options chain OI=0 for near-expiry** — Volume data available but no OI on yfinance. Use Tradier provider (requires `TRADIER_TOKEN`) for OI + greeks.

## Feature Requests

- [ ] **Peer comparison endpoint** — `ofclient peers <TICKER>` returning 3-5 comparable companies (currently need web_search).
- [x] **Sector ETF batch quote** — `ofclient quotes XLK XLF XLV` already works — use `quotes` (plural) for batch. (2026-06-24)
- [x] **Options chain OI from alternative provider** — Tradier provider integrated as lead for `options_expirations` and `option_chain`; returns ORATS greeks + OI for all strikes. Requires `TRADIER_TOKEN`. (2026-06-24)
- [x] **Earnings date endpoint** — `ofclient earnings-date <TICKER>` added; returns next scheduled report date, quarter, time-of-day, and consensus estimates. (2026-06-24)
- [ ] **Dividend-adjusted vs unadjusted price flag** — `PriceBar` already carries both `close` and `adj_close`; a `--adjusted`/`--unadjusted` flag on `ofclient price` to select which field to surface hasn't been added.
