# ofclient Feedback & Feature Requests

## Open Issues

- [ ] **KR/JP price discrepancy** — 000660.KS and 285A.T fall back to yfinance. Adjusted closes differ materially from quote (Kioxia 10%+ gaps).

## Deferred — needs live API access to verify, not building blind

(2026-07-03 triage of external "ofclient Improvement Requests" doc, 22 items — 7 fixed, 9 already resolved and dropped from this file, these 5 remain)

- **A2 (FMP silent volume corruption)** — cross-provider volume sanity-checking needs real FMP vs yfinance responses to calibrate a tolerance band; a wrong threshold is worse than no check (false positives dropping good data). Needs a session with `FMP_API_KEY` set to observe actual corrupted payloads and design the check against real data.
- **B1 (treasury/yield-curve endpoint)** — new endpoint + new FMP `treasury-rates` (or FRED) integration; no existing code path to extend, needs to be designed against real API responses.
- **B2 (13F institutional holdings returns `{"data": [], "rows": 0}` with no error)** — `FMPProvider.get_institutional_holders` does `if not data or not isinstance(data, list): return []`, which silently swallows both "genuinely zero holders" and "malformed/error response body" (FMP errors typically come back as a dict, e.g. `{"Error Message": ...}`, which fails the `isinstance(data, list)` check and also returns `[]`). Distinguishing those without seeing a real error payload risks guessing the wrong shape — deferred pending a live 402/403 sample.
- **B4 (Cboe volatility-index provider for VIX3M/VXSMH/VXN/RVX)** — new provider from scratch against `cdn.cboe.com/api/global/delayed_quotes/...`; the price=0 garbage from these symbols is no longer trusted (fixed), but there's still no actual coverage. Needs to be built and tested against the live Cboe endpoint.
- **B5 (batch company-profile endpoint)** — `get_info` is per-symbol only; a batch `get_infos` mirroring `get_quotes`' fan-out pattern is a reasonable design but multiplies provider call volume during testing without keys to verify the batch response shapes across providers.
