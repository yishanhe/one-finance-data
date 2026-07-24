# OneFinance / ofclient Feedback

Updated: 2026-07-19

## Open Issues

None.

<details>
<summary>Original report: Options OI reliability (resolved 2026-07-19, kept for reference)</summary>

- [x] **Options OI reliability: yfinance/ofclient returns near-zero open interest on actively traded chains** (reported 2026-07-08 23:42 PDT)

  ### Context
  During MU options-chain analysis after the 2026-07-08 close, `ofclient options` and `ofclient options-analytics` returned clearly unreliable open-interest data for near-term expirations, while option volume was very large.

  Example commands:

  ```bash
  ofclient quote MU --format json
  ofclient options MU --format json
  ofclient options MU --expiration 2026-07-10 --format json
  ofclient options-analytics MU --format json
  ```

  Observed quote:
  - MU spot: `$948.80`
  - Quote timestamp: `2026-07-08T20:00:00Z`

  Observed `options-analytics` output:
  - `total_call_volume`: `210,364`
  - `total_put_volume`: `184,682`
  - `total_call_oi`: `3`
  - `total_put_oi`: `5`
  - `pcr_volume`: `0.8779`
  - `pcr_oi`: `1.6667`
  - `source`: `yfinance`

  For an actively traded MU chain with >390k total contracts traded across the first six expirations, total OI of only 8 contracts is not plausible.

  Per-expiration checks showed the same failure pattern: near-the-money strikes around `$900-$1,100` had large volume but `open_interest: 0` across calls and puts. Examples from 2026-07-10 expiry:
  - `$1000C` volume ~23,033, OI 0
  - `$950C` volume ~14,483, OI 0
  - `$900P` volume ~19,259, OI 0
  - `$950P` volume ~8,206, OI 0

  ### Impact
  This makes the following outputs unreliable or unusable:
  - call wall / put wall
  - put/call OI ratio
  - max pain
  - gamma exposure / GEX
  - gamma flip level
  - dealer positioning interpretation

  The consumer-side options analysis had to fall back to volume and premium-flow only, explicitly refusing to publish OI walls/GEX because the OI source was invalid.

  ### Requested improvements
  1. Add an OI sanity check in `ofclient options` / `options-analytics`:
     - if total volume is large but total OI is near zero, flag `oi_reliable: false`.
     - if many near-the-money strikes have `volume > 0` but `open_interest == 0`, flag likely OI truncation/staleness.

  2. Add metadata fields:
     - `oi_reliable: true/false`
     - `oi_as_of`
     - `oi_source`
     - `oi_stale_reason` or `oi_warning`

  3. In `options-analytics`, avoid returning a normal-looking `pcr_oi` when OI totals are implausibly tiny. Return `pcr_oi: null` plus warning instead.

  4. Consider fallback/cross-check providers for OI, especially for high-priced or high-volatility tickers where yfinance may truncate OI.

  5. If only volume is reliable, expose this clearly so downstream consumers can switch to volume/premium-flow mode without mistaking it for OI-based structure.

</details>

## Resolved

- [x] **Options OI reliability** (2026-07-08 report, resolved 2026-07-19) — Yahoo can
  intermittently zero OI (OCC disseminates OI once daily pre-market; worst after volatile
  sessions — the same MU chain showed healthy OI on 07-19), and the client originally had no
  plausibility defense. Fixes: `assess_oi_reliability` in `options/core.py` (aggregate volume-vs-OI check +
  per-contract truncation check); `OptionsAnalytics` gains `oi_reliable` / `oi_warning` and
  forces `pcr_oi: null` when unreliable (volume metrics stay usable — requests #1, #2, #3, #5);
  `compute_max_pain` now raises on all-zero OI instead of silently returning the lowest strike;
  cross-provider OI reconciliation is not built.

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
