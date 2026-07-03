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

## 2026-07-03 — Triage of external "ofclient Improvement Requests" doc

Consolidated skill-library feedback (A1–D6, 22 items) triaged against current code. Three buckets below. No live API keys available this session, so anything requiring a real provider response to verify was deferred rather than built blind.

### Fixed this session

- [x] **A1 — finnhub/others return price=0.0 for symbols they don't really carry** (e.g. `VIX3M`) instead of erroring. `get_quote` now raises `NotSupportedError` when a provider's own result has `price <= 0`, reusing the existing per-symbol 24h negative-cache + tier-fallback — the next provider gets a chance instead of the client returning garbage as if it were real. Batch `get_quotes` drops such entries before they're cached (comes back as `BATCH_RESULT_MISSING` for that symbol instead of a silent `price: 0`). Doesn't cover the Cboe-index-provider half of A1 — see deferred.
- [x] **A4/A5 — stale quotes (yfinance intraday, ADR previous-day timestamps) not flagged.** `quote`/`quotes` now include `price_age_seconds` per quote (`now - timestamp`), computed at the CLI layer so it works for every provider without a model change. Callers can now detect "this price is actually yesterday's" instead of trusting `price` at face value.
- [x] **C1 residual — `ofclient quote NVDA,AMD` silently misbehaves.** The batch command (`ofclient quotes SYM1 SYM2 ...`) already existed and works; the singular `quote` command now rejects comma/space-containing input up front with `InvalidArgumentError` pointing at `quotes`, instead of the malformed symbol propagating deep into the provider stack.
- [x] **C4 — quote lacks prev_close/change_pct, forcing an extra `price` call per ticker.** Added optional `prev_close`/`change_pct` fields to `Quote`. Populated from finnhub (`pc`), FMP (`previousClose`), yfinance (`previousClose`/`regularMarketPreviousClose`), and Massive (`prevDay.c`) — all four compute `change_pct` the same way via a shared `change_pct_from_prev_close()` helper rather than trusting each provider's own (differently-named/rounded) change field. Alpha Vantage/Tradier `get_quote` untouched — low-traffic tier positions, field defaults to `None` there, non-breaking.
- [x] **D1 — `indicators` hard-errors under 5 bars, breaking on new listings/SPACs.** `compute_indicators` now only raises on truly empty input (0 bars); 1–4 bars returns a partial result (most fields `None`, since the underlying per-indicator helpers already degrade gracefully) with a new `insufficient_history: bool` flag so callers can distinguish "thin history" from "computation failed."
- [x] **D6 — `options` without `--expiration` returns bare date strings, trips up first-time callers.** Expanded the command docstring to state the two-mode behavior explicitly up front (dates-list vs actual chain) rather than relying on the `--expiration` option's help text alone.
- [x] **B3 — `screen '<sector query>'` 404s against FMP with no guidance.** `screen`'s docstring now explicitly says to use `ofclient sector <name>` for sector-level overviews and that a sector-only query string will typically 404 the ticker-level screener endpoint.

### Already resolved / stale against current code (verified, not rebuilt)

- **C2 (schema envelope inconsistency)** — `make_envelope`/`make_error_envelope`/`make_dry_run_envelope` in `cli/format.py` are the single source of every CLI response; every command routes through `_emit`/`_error_exit`. One shape, always, in the current codebase.
- **C5 (`price` response shape varies)** — `price` always goes through `make_envelope` (`{"data": [...]}`), never a bare array.
- **C3 (concatenated JSON on provider fallback)** — no code path in this repo writes to stdout outside `print_json`/`print_table`/`print_csv` (checked `router.py`, all `providers/*.py`, `cache/manager.py` for stray `print`/`echo`). Each CLI invocation emits exactly one JSON envelope. Almost certainly an artifact of a per-symbol shell loop in the *calling* skill concatenating multiple invocations' stdout, not an ofclient bug — worth fixing at the skill layer, not here.
- **D5 (options OI truncation on yfinance)** — already resolved 2026-06-24 (see "Options chain OI=0" above): Tradier leads the options tiers now and returns full-chain OI + ORATS greeks.
- **D4 (earnings next-date detection fragile)** — already resolved 2026-06-24: `ofclient earnings-date <TICKER>` returns the next scheduled date directly; the "latest null eps_actual row" heuristic this item describes shouldn't be needed anymore.
- **C1 (no multi-ticker quote)** — `ofclient quotes SYM1 SYM2 ...` already exists and batches server-side; only the singular-command UX gap was real (fixed above).
- **D3 partial (adjusted vs unadjusted prices)** — `--adjusted` on `ofclient price` already resolved this for price history 2026-06-24. The remaining part of D3 (KR/JP absolute-value divergence itself) is the pre-existing open "KR/JP price discrepancy" item above — provider data quality, not an ofclient bug to fix.
- **A3 (finnhub quote volume always 0)** — substantially mitigated already, just not under this name: `AugmentConfig` treats `volume == 0` as a missing field (`_is_missing`) and auto-fills it from a fallback provider (yfinance) on every finnhub quote, merging `source` to `"finnhub+yfinance"`. The residual ask (make `Quote.volume` nullable so a true zero is distinguishable from "no data") touches a non-optional field every provider sets — deferred below, low residual value given the augment fallback already covers the common case.
- **D2 (`indicators.as_of` unreliable)** — `as_of` is already always populated from `bars[-1].date` when bars exist (`null` only when `bars` is genuinely empty, which is now a real, documented error case per D1's fix). The staleness this item actually describes is upstream provider staleness (same root cause as A4/A5), not a bug in how `as_of` is computed.
- **C6 (FMP rate-limit opacity)** — `ofclient providers status` already exists and surfaces exactly this: `available`, `cooldown_remaining_s`, `last_error`, `consecutive_failures` per provider (`ProviderState.to_dict()` in `router.py`). Not documented prominently enough for the skill authors to have found it, apparently — worth cross-linking from reporter skills rather than building anything new.

### Deferred — needs live API access to verify, not building blind

- **A2 (FMP silent volume corruption)** — cross-provider volume sanity-checking needs real FMP vs yfinance responses to calibrate a tolerance band; a wrong threshold is worse than no check (false positives dropping good data). Needs a session with `FMP_API_KEY` set to observe actual corrupted payloads and design the check against real data.
- **B1 (treasury/yield-curve endpoint)** — new endpoint + new FMP `treasury-rates` (or FRED) integration; no existing code path to extend, needs to be designed against real API responses.
- **B2 (13F institutional holdings returns `{"data": [], "rows": 0}` with no error)** — `FMPProvider.get_institutional_holders` does `if not data or not isinstance(data, list): return []`, which silently swallows both "genuinely zero holders" and "malformed/error response body" (FMP errors typically come back as a dict, e.g. `{"Error Message": ...}`, which fails the `isinstance(data, list)` check and also returns `[]`). Distinguishing those without seeing a real error payload risks guessing the wrong shape — deferred pending a live 402/403 sample.
- **B4 (Cboe volatility-index provider for VIX3M/VXSMH/VXN/RVX)** — new provider from scratch against `cdn.cboe.com/api/global/delayed_quotes/...`; A1's fix above stops the *garbage* (price=0) from being trusted, but doesn't add actual coverage for these symbols. Needs to be built and tested against the live Cboe endpoint.
- **B5 (batch company-profile endpoint)** — `get_info` is per-symbol only; a batch `get_infos` mirroring `get_quotes`' fan-out pattern is a reasonable design but multiplies provider call volume during testing without keys to verify the batch response shapes across providers.
