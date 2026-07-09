# OneFinance / ofclient Feedback

Updated: 2026-07-08

## Open Issues

None.

## Resolved

- [x] **indicators staleness / support misclassification** (2026-07-08 external report) —
  `TechnicalIndicators` now carries `as_of` / `computed_at` / `last_close`, live-quote-classified
  `support_levels_current` / `resistance_levels_current` (`reference_price` from a 30s-cached
  quote; `--no-quote` to skip), and a trading-calendar-aware `indicator_stale` + `stale_reason`
  flag. `support_levels` kept but documented as classified vs the last bar close. The
  consumer-side workaround in the market-close-report script can be removed.
- [x] **C1** — one plan-gated 403 benched a provider's whole endpoint for 24h; `endpoint_ok`
  marker (7-day TTL) now vetoes the global bench, and a success heals an existing one.
- [x] **C2** — quote augment stalls (up to 17.6 s to fill `volume`); filler calls now run under
  `AugmentConfig.timeout_s` (2 s) in daemon threads with background augment-cache write-behind.
- [x] **C4** — exhausted requests now write a terminal `all_failed` audit row;
  `audit stats` reports `failed_requests` / `failed_requests_by_endpoint`.
- [x] **C5** — index aliases (yfinance `SOX`→`^SOX` etc.; cboe accepts `^VIX`); `Symbol` model
  pattern now allows a leading caret (caret quotes previously crashed at model construction).
- [x] **C6** — cboe `change_pct` no longer fabricated as 0.0; derived from `price_change` /
  `price_change_percent` instead of the broken `prev_day_close`.
- [x] **C7** — `providers check` reports `plan_gated` endpoints from live global 402/403
  negative-cache entries.
- [x] **KR/JP price discrepancy** — yfinance price history requests unadjusted OHLC
  (`auto_adjust=False`); `close` stays comparable to quote price, `adj_close` carries Yahoo's
  adjusted close.
- [x] **B1** (treasury/yield-curve endpoint), **B2** (13F empty envelope vs error),
  **B4** (Cboe volatility-index provider), **B5** (batch company profiles) — see git history.

## Deferred

- **C3 (yfinance latency collapses under sequential burst — 400 ms → 6–17 s)** — the
  worst user-facing impact came through the augment path, which C2's budget now caps.
  The remaining slowness lives inside the yfinance library (likely Yahoo throttling /
  crumb refresh under rapid calls) and is not observable from our layer — no
  http_status or retry counts reach the audit log. Fixing blind risks regressions
  (e.g. switching `Ticker.info` → `fast_info` drops bid/ask). Revisit with targeted
  instrumentation of a burst workload if it stays painful now that augment no longer
  amplifies it.

- **A2 (FMP silent volume corruption)** — cross-provider volume sanity-checking needs
  real FMP vs yfinance responses to calibrate a tolerance band; a wrong threshold is
  worse than no check (false positives dropping good data). Needs a session with
  `FMP_API_KEY` set to observe actual corrupted payloads and design the check against
  real data. (Carried over from 2026-07-03 triage.)
