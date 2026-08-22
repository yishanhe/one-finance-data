# Improvement Plan — Performance / Cache Hit Rate & Functionality

*Audit date: 2026-07-01. All phases complete as of 2026-08-21.*

## Status

| Phase | Items | Status |
|---|---|---|
| 1 | P1 step 1, P2-A, F1 | ✅ done (commit `3445b58`) |
| 2 | P1 step 2, P3, P4, P6 | ✅ done |
| 3 | F2 (GEX/max-pain), F3 warm | ✅ done (commit `946f513`) |
| 4 | P5, F4, F5, F6 | ✅ done (commit `b2b0dce`) |

## Completed Items

| ID | Description |
|---|---|
| P1 | Trading-day-aware coverage + delta-fetch for daily price history |
| P2 | Augment filler result cached separately (`quote_volume:{sym}`, 5-min TTL) |
| P3 | Router cooldown state persisted cross-process via diskcache |
| P4 | Endpoint-wide negative cache for plan-gated 402/403 errors |
| P5 | Serialization cost cuts: `model_dump(mode="json")`, in-process memo layer above diskcache, audit-log file handle reused |
| P6 | Holiday-aware market clock; removed dead UTC-5 fallback |
| F1 | Correctness fixes: batch-result symbol matching; empty-list `FinanceError` instead of `IndexError` |
| F2 | Options analytics tier: GEX, max-pain, SVIX, `oi_reliable` flag |
| F3 | `ofclient warm` cache warm command |
| F4 | `onefinance.pandas` DataFrame convenience layer (`bars_to_df`, `quotes_to_df`, `financials_to_df`, `indicators_to_df`, `options_to_df`, `to_df`) |
| F5 | `client.batch(...)` thread-pool helper + `AsyncOneFinanceClient` async wrapper |
| F6 | Documented best-effort intraday behavior (`interval != "1d"` skips subsumption) |
