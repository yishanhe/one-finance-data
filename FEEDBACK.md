# OneFinance / ofclient Feedback

Updated: 2026-08-25

## Open Issues

- [ ] **Yahoo price-history end boundary omits the expected close** (verified 2026-09-21)

  `YFinanceProvider.get_price_history` documents an inclusive date range but passes `end.isoformat()` unchanged to Yahoo's exclusive `history(end=...)`. Both the repository and installed `/opt/homebrew/lib/python3.14/site-packages/onefinance/providers/yfinance_provider.py` contain this behavior. The client promises `[start,end]` but cannot restore the omitted bar; range-extension code may also skip boundary dates by advancing from the requested end instead of actual covered dates.

  Live reproduction after the 2026-09-21 close: `ofclient price QQQ --range 2y --format json` ended at 2026-09-18 (498 rows); `ofclient price QQQ --start 2024-09-21 --end 2026-09-22 --format json` returned the genuine 2026-09-21 OHLCV (500 rows, close 741.469970703125). All six QQQ-model inputs recovered with explicit dates. No quote-to-bar synthesis or `--no-cache` used.

  Downstream mitigation deployed only in investor qqq-indicator/daily_decision.py: request expected_session + one calendar day, clamp model bars to expected_session, preserve the old-data block, retain request provenance. Upstream fix remains OPEN: normalize inclusive provider boundaries and audit exact/covering/extension caches, including existing falsely covered ranges. Add end-inclusion, adjacent-range extension, and post-close freshness tests before releasing. Do not silently install a global package change from a single-profile repair.

  Separately, CLI `metadata.cache_hit` is currently `not effective_no_cache`, not evidence of a real cache hit. Use audit data / provider bar timestamps when diagnosing freshness; this flag alone cannot establish the cause.

- [ ] **`ofclient macro` — ALL_PROVIDERS_FAILED with FMP_API_KEY present** (reported 2026-08-15)

  **Diagnostics fixed 2026-08-25; data availability remains open.** The command now reports
  each unavailable provider instead of an empty message. Current live result identifies
  Finnhub as cached unsupported and FMP as plan-gated (HTTP 402); no unsupported YFinance
  fallback is attempted.

  ```bash
  ofclient macro --start 2026-08-15 --end 2026-08-29 --country US --format json
  # {"status": "error", "error": {"code": "ALL_PROVIDERS_FAILED", "message": "All providers failed for 'economic_calendar': "}}
  ofclient providers check   # confirms fmp api_key_present: true
  ```

  Empty error message (no per-provider detail like `sentiment` gave), so couldn't tell which
  provider(s) were tried or why they failed. Reproducible across several date ranges and with
  no `--country` filter too. A downstream skill (`reporters/weekly-events`) currently uses a
  free ForexFactory JSON feed for this and would consider switching to `ofclient macro` if it
  worked, for schema consistency with the rest of the pipeline — not urgent since the FF feed
  works fine, but flagging since the command exists and silently can't serve its stated purpose.

## Deferred

- **C3 (yfinance latency collapses under sequential burst — 400 ms → 6–17 s)** — the
  worst user-facing impact came through the augment path, which C2's budget now caps.
  The remaining slowness lives inside the yfinance library (likely Yahoo throttling /
  crumb refresh under rapid calls) and is not observable from our layer — no
  http_status or retry counts reach the audit log. Fixing blind risks regressions
  (e.g. switching `Ticker.info` → `fast_info` drops bid/ask).

  **Investigated 2026-08-15, not reproduced.** Ran `YFinanceProvider` directly
  (bypassing cache) against live Yahoo: 30 sequential `get_info` calls across a mixed
  large-cap/meme/ETF symbol set (190–415 ms each, no spikes), 12 sequential
  `get_quote` calls (190–400 ms each), and a 10-way concurrent `get_quote` burst via
  `ThreadPoolExecutor` (620–885 ms each, 885 ms wall clock for all 10 — no collapse).
  No 6–17 s outliers in any run. Likely a transient Yahoo-side throttling incident
  rather than a standing condition — or C2's augment-budget fix already masks most of
  the user-facing impact. Keep deferred; revisit only if a fresh occurrence is
  reported with a timestamp so it can be correlated with real traffic (a one-off
  burst test can't catch an intermittent condition).

- **A2 (FMP silent volume corruption)** — cross-provider volume sanity-checking needs
  real FMP vs yfinance responses to calibrate a tolerance band; a wrong threshold is
  worse than no check (false positives dropping good data). Needs a session with
  `FMP_API_KEY` set to observe actual corrupted payloads and design the check against
  real data. (Carried over from 2026-07-03 triage.)

  **Investigated 2026-08-15 with live `FMP_API_KEY`, not reproduced.** Compared FMP
  vs yfinance daily-bar volume for AAPL/MSFT/TSLA/NVDA plus a thinner/meme/ETF set
  (SIRI, PLTR, SOFI, BABA, NIO, SPY, CCL, F, GME, AMC, QQQ, IWM) across three windows
  (last 10 trading days, the original 2026-07-03 triage window, and the same calendar
  window one and two years back) — every comparable ratio landed at 1.000–1.002
  (rounding-level agreement, no corruption). Also checked live `get_quote` volume for
  6 symbols — same result. Separately confirmed a real but unrelated finding: FMP's
  `historical-price-eod/full` 402s per-symbol for GME/AMC/QQQ/IWM/MU under this API
  plan tier ("this value set for 'symbol' is not available under your current
  subscription") — not corruption, and already handled correctly by the existing
  per-symbol negative-cache + `endpoint_ok` healing (client falls through to the next
  tier; a success on AAPL doesn't let the per-symbol 402 escalate to a global bench).
  No corrupted payload found to calibrate a threshold against — recommend closing
  unless a new report supplies a concrete symbol/date.
