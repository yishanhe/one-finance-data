# Improvement Plan — Performance / Cache Hit Rate & Functionality

*Audit date: 2026-07-01. Based on code review of `client.py`, `cache/manager.py`, `core/router.py`, `core/config.py`, `providers/base.py`, `providers/_http.py`, `audit/log.py`, plus real usage data from the audit log (last 24h).*

## Baseline (measured, last 24h)

- 407 provider calls, 156 cache hits → **34% request-level hit rate**. No errors, no fallbacks.
- Call distribution: `quote` 248, `price_history` 100, `option_chain` 32, `options_expirations` 16, everything else single digits.

Three dominant call sinks:

1. **Quote volume-augment doubles quote cost.** Finnhub `/quote` returns no volume, so `AugmentConfig` (`quote: ["volume"]`) fires a yfinance secondary call on *every* finnhub quote miss. Measured: 106 finnhub quote successes → 104 yfinance augment calls — ~26% of all HTTP traffic spent filling one field.
2. **`price_history` refetches daily despite range subsumption.** Default `end=today` rolls the requested range forward each day, so yesterday's cached superset `[start, yesterday]` never covers today's `[start, today]` → full refetch of the whole range every day, including weekends when zero new bars exist.
3. **Cooldown state is in-memory only** (`router.py` `ProviderState`). Every CLI invocation is a fresh process, so a dead provider is retried at tier-0 on every run and exponential backoff never accumulates.

---

## Plan 1 — Performance / cache hit rate

Ordered by measured impact.

### P1. Trading-day-aware coverage + delta-fetch for daily price history *(biggest win)*

- **Step 1 (cheap):** in `CacheManager.find_covering_price_range`, treat a cached range `[s, e]` as covering a request `[s, today]` when no trading day exists in `(e, today]` (weekend / holiday / pre-open). Kills all weekend and evening refetches.
- **Step 2 (delta-fetch):** on partial overlap (cached `[s, e1]`, requested `[s, e2]`, `e2 > e1`), fetch only `[e1+1, e2]` from the provider, merge with cached bars, store the extended range and re-register it in the range index. Turns the daily "1y ending today" pattern from a 250-bar refetch into a 1-bar fetch.
- Estimated effect: `price_history` provider calls drop ~80–90%.

### P2. Kill the quote volume-augment double-call

Options:

- **A (recommended):** cache the augment filler separately — a per-symbol `quote_volume:{sym}` key with ~5-min TTL. Volume moves slowly relative to the 30s quote TTL, so one yfinance call serves ~10 quote refreshes. The augment path checks this cache before making an HTTP call.
- B: make augment opt-in per call (`need_volume=True`).
- C: reorder the quote tier yfinance-first (loses finnhub real-time quality — not recommended).

Estimated effect: ~100 augment calls/day → ~10.

### P3. Persist router cooldown state cross-process

Store `ProviderState` (`cooldown_until`, `consecutive_failures`, `last_error`) in diskcache alongside the negative cache. CLI runs stop paying tier-0 timeout latency on a known-dead provider, and exponential backoff actually accumulates as designed.

### P4. Endpoint-wide negative cache for plan-gated errors

FMP 402 / Finnhub 403 are *symbol-independent* plan restrictions, but the negative cache keys per-symbol, so each new symbol pays a discovery HTTP call (19 `not_supported`/day from FMP measured). When `NotSupportedError.http_status` ∈ {402, 403}, also write a symbol-free key `not_supported:{provider}:{endpoint}:` and check it first in `ProviderRouter.dispatch`.

### P5. Serialization cost cuts (in-process latency, not API calls)

- `_serialise_envelope`: replace `json.loads(item.model_dump_json())` per item with `item.model_dump(mode="json")`. Halves serialize work for 250-bar lists.
- Optional: small in-process LRU (dict, ~100 entries, TTL-respecting) above diskcache — skips JSON parse + Pydantic `model_validate` on repeated hits. Matters for `get_quotes` loops and `get_indicators`.
- `AuditLog.record` opens/closes the file per entry — keep the handle open, flush per line.

### P6. Holiday-aware market clock

`is_market_open_now` ignores NYSE holidays → wrong short TTLs on holidays (30s quote TTL on July 4th) and blocks P1 step 1. Vendor a static NYSE holiday table (~10 dates/year) or use `exchange_calendars`. Also: the zoneinfo-`ImportError` fallback uses fixed UTC-5 (wrong during DST) and is dead code on Python 3.11+ — delete it.

---

## Plan 2 — Functionality

### F1. Correctness fixes first (small, real)

- `_cached_batch_fetch` (`client.py`, zip at step 3): `zip(missing_symbols, batch_result)` assumes the provider preserves order and drops nothing. A provider that omits one invalid symbol mid-list misassigns every quote after it to the wrong symbol. Fix: match by `quote.symbol`, fall back to positional pairing only when lengths match.
- `_single` (`client.py`): an empty list → `IndexError` leaks a raw exception through `get_quote` / `get_info` / `get_dcf` / `get_analyst_data`. Raise a proper `FinanceError` ("provider returned empty result") instead.

### F2. Options analytics tier (GEX / max-pain / SVIX)

The analytics layer remains provider-agnostic:

- `get_gex(symbol)` — per-strike gamma × OI from any chain carrying gamma, net dealer gamma, flip point.
- `get_max_pain(symbol, expiration)`.
- SVIX module with CBOE strike selection (zero-bid truncation, K0), currently not implemented.
- CLI: `ofclient gex AAPL`, `ofclient maxpain AAPL`.

### F3. Cache warm command

`ofclient warm SPY QQQ AAPL --endpoints quote,price,indicators` — prefetch a watchlist in one shot, concurrent. Pairs with P1: a cron warm before market open makes intraday requests all cache hits.

### F4. DataFrame convenience layer

Optional `onefinance.pandas` module: `bars_to_df(bars)`, `financials_to_df(...)`, or `client.get_price_history(..., as_df=True)`. Agents and notebooks both want this; today every consumer rewrites the same loop.

### F5. Async client

`AsyncOneFinanceClient` on `httpx.AsyncClient`. Biggest structural add — router and cache layers need async variants. Worth it only if agent workloads fan out heavily; schedule after F2/F3. Cheaper interim: expose the existing thread-pool batch pattern (`get_options_analytics` style) as a generic `client.batch(...)`.

### F6. Intraday support hardening

`interval != "1d"` today: no subsumption, no smart TTL beyond the market-open check, provider bar-count caps silently truncate. Either document as best-effort or add bar-count-aware caching. Low priority until an intraday consumer exists.

---

## Suggested order

| Phase | Items | Effort | Payoff | Status |
|---|---|---|---|---|
| 1 | P1 step 1, P2-A, F1 | ~1 day | hit rate 34% → ~60%+, bug fixes | ✅ done (commit `27f0117`→`3445b58`) |
| 2 | P1 step 2, P3, P4, P6 | ~2 days | hit rate → ~80%, robust CLI behavior | ✅ done (same commit as phase 1) |
| 3 | F2 (GEX/max-pain), F3 warm | ~2–3 days | new capability tier | ✅ done (`946f513`); F3 warm landed alongside phase 1 |
| 4 | P5, F4, then F5 if needed | as-needed | latency + ergonomics | ⬜ not started |

**P5 done** (commit `96740af`): audit-log file handle reused (line-buffered, closed only in `close()`/`clear()`) and an in-process memo layer added above diskcache (256-entry cap, 5s staleness cap, purged on `invalidate_by_type`/`clear`).

Remaining open items: **F4** (pandas convenience layer), **F5** (async client), **F6** (intraday hardening — intentionally deferred, low priority).
