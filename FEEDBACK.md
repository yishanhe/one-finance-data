# ofclient Feedback & Feature Requests

## Existing Issues

- [x] **OI data missing for high-priced stocks (MU, etc.)** — yfinance OI returns 0 for strikes above ~$995 on MU. Resolved: Tradier is now the lead options provider (`options_expirations` + `option_chain` tiers), returning ORATS greeks + OI for all strikes. Requires `TRADIER_TOKEN`. (2026-06-24)
- [x] **`--no-cache` on quote is unnecessary** — quote already has 30s TTL, `--no-cache` just wastes API quota. Fixed in reporter skills. (2026-06-19)
- [x] **ofclient news returns empty headlines** — yfinance provider now skips articles with empty titles; if all raw items had empty titles it raises `ProviderError` to fall through to FMP/Finnhub. (2026-06-24)
- [x] **ofclient analyst for INTC returns empty** — Root cause: FMP `get_analyst_data`, `get_corporate_actions`, and `get_institutional_holders` were passing full URLs to `_get()` which prepended the base URL a second time, producing malformed double-URL requests. Fixed by passing only the path suffix. (2026-06-24)
- [x] **`financials --period quarterly` no `--limit`** — Added `--limit` / `-n` to `ofclient financials`. Returns most-recent N periods. (2026-06-24)
- [ ] **KR/JP price discrepancy** — 000660.KS and 285A.T fall back to yfinance. Adjusted closes differ materially from quote (Kioxia 10%+ gaps).
- [x] **ofclient estimates opaque period labels** — yfinance now maps `0q`→`current_Q`, `+1q`→`next_Q`, `0y`→`current_FY`, `+1y`→`next_FY`, etc. FMP already emits proper `fiscal_date` + `YYYY-FY` labels. (2026-06-24)
- [x] **Options chain OI=0 for near-expiry** — Volume data available but no OI on yfinance. Resolved: Tradier is now the lead options provider, returns OI + ORATS greeks for all strikes including near-expiry. Requires `TRADIER_TOKEN`. (2026-06-24)

## Feature Requests

- [x] **Peer comparison endpoint** — `ofclient peers <TICKER>` added. FMP lead (returns name + price + market_cap), Finnhub fallback (symbols only). TTL 7 days. (2026-06-24)
- [x] **Sector ETF batch quote** — `ofclient quotes XLK XLF XLV` already works — use `quotes` (plural) for batch. (2026-06-24)
- [x] **Options chain OI from alternative provider** — Tradier provider integrated as lead for `options_expirations` and `option_chain`; returns ORATS greeks + OI for all strikes. Requires `TRADIER_TOKEN`. (2026-06-24)
- [x] **Earnings date endpoint** — `ofclient earnings-date <TICKER>` added; returns next scheduled report date, quarter, time-of-day, and consensus estimates. (2026-06-24)
- [x] **Dividend-adjusted vs unadjusted price flag** — Added `--adjusted` flag to `ofclient price`; when set, `close` in each output bar is replaced with `adj_close` so downstream consumers keying on `close` get dividend-adjusted prices. Both fields still present in output. (2026-06-24)
