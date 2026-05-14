# OneFinance — TODO

## M1 — models + BaseProvider + YFinanceProvider ✅
- [x] `pyproject.toml`, package structure
- [x] `onefinance/core/models.py` — 11 Pydantic models (frozen, extra-forbid)
- [x] `onefinance/core/errors.py` — full error hierarchy with codes
- [x] `onefinance/providers/base.py` — ABC with 7 endpoint stubs + `supports()`
- [x] `onefinance/providers/yfinance_provider.py` — `get_price_history` + `get_info`
- [x] 60 unit tests, all passing

## M2 — CacheManager + integration into M1 path ✅
- [x] `onefinance/cache/keys.py` — `make_key()`, param hashing (provider-agnostic)
- [x] `onefinance/cache/manager.py` — `CacheManager` (diskcache wrapper)
  - [x] `get()` / `set()` with TTL + JSON envelope serialization
  - [x] TTL-by-endpoint logic (§6 freshness taxonomy)
  - [x] Smart TTL for price history (`ttl_for_price_history`)
  - [x] `is_market_open_now()` helper (US exchange hours)
  - [x] Tag-based invalidation (`invalidate_by_type`)
  - [x] `stats()` — entries, size, hits/misses
- [x] 37 new tests (cache keys + cache manager), all passing

## M3 — OneFinanceClient skeleton with single-provider routing ✅
- [x] `onefinance/core/client.py` — `OneFinanceClient` public API
  - [x] Constructor: providers list, cache init, context manager
  - [x] All 7 endpoints with cache integration
  - [x] Per-call overrides: `no_cache`, `provider`, `ttl`
  - [x] Linear provider walk with fallback
- [x] 17 new client tests, all passing

## M4 — FMPProvider for all endpoints ✅
- [x] `onefinance/providers/fmp.py` — FMP `/stable/` API adapter
  - [x] `get_price_history` — `/stable/historical-price-eod/full`
  - [x] `get_quote` — `/stable/quote`
  - [x] `get_info` — `/stable/profile`
  - [x] `get_financials` — income-statement, balance-sheet, cash-flow
  - [x] `get_ratios` — `/stable/ratios`
  - [x] `get_earnings` — `/stable/earnings`
  - [x] `get_insider_trades` — `/stable/insider-trading`
  - [x] Rate-limit detection (HTTP 429, "Limit Reach", error body)
  - [x] Free tier compatible (limit=5)
- [x] 28 unit tests with mocked httpx (all passing)
- [x] 5 integration tests hitting real FMP API (all passing)
- [x] **Total: 142 unit + 5 integration = 147 tests passing**

## M5 — Router with tier walking + cooldowns ✅
- [x] `onefinance/core/router.py` — `ProviderRouter` with tier walking
  - [x] `ProviderState` dataclass — cooldown tracking, consecutive failures
  - [x] `dispatch()` — walks tier list, skips cooldown providers, handles fallback
  - [x] Exponential backoff: `min(cooldown_s × 2^failures, max_backoff)`
  - [x] Rate-limit errors → provider cooldown + try next tier
  - [x] Generic errors → cooldown + try next tier
  - [x] `NotSupportedError` → silently skip (no cooldown)
  - [x] Forced provider (`provider_name=`) bypasses cooldown
  - [x] `state()` — returns all provider states for diagnostics
  - [x] `reset_cooldowns()` — testing helper
- [x] `onefinance/core/config.py` — YAML config loader
  - [x] `OneFinanceConfig` — tiers, providers, cache, cooldown settings
  - [x] `ProviderConfig` — API key env resolution, timeouts
  - [x] `get_tier_list()` — handles Type A/B (flat) and Type C (default/fresh)
  - [x] `load_config()` — YAML file loading with fallback to defaults
  - [x] Default tiers match design doc §6
- [x] `onefinance/core/client.py` — upgraded to use `ProviderRouter`
  - [x] Accepts `config` parameter (YAML path or `OneFinanceConfig`)
  - [x] Exposes `providers` property for state inspection
  - [x] `_cached_fetch()` delegates to `router.dispatch()`
  - [x] Type C endpoints pass `fresh=` through to router
- [x] 24 router tests + 20 config tests = 44 new tests, all passing
- [x] **Total: 186 unit tests passing (zero regressions)**

## M6 — FinnhubProvider (full coverage) ✅
- [x] `onefinance/providers/finnhub.py` — Finnhub API adapter
- [x] All 7 endpoints (get_price_history, get_quote, get_info, get_financials, get_ratios, get_earnings, get_insider_trades)
- [x] Rate-limit detection (HTTP 429, `Retry-After` header)
- [x] 28 unit tests with mocked httpx, all passing

## M7 — TwelveDataProvider (partial coverage, price-focused) ✅
- [x] `onefinance/providers/twelve_data.py`
- [x] `get_price_history` + `get_quote`
- [x] Rate-limit detection (HTTP 429 + `"code": 429` in body)
- [x] 15 unit tests with mocked httpx, all passing

## M8 — Telemetry, README, example notebook (skipped)
- [ ] README with usage examples
- [ ] Example Jupyter notebook

## M9 — CLI: data commands, JSON-first output, structured errors ✅
- [x] `onefinance/cli/format.py` — envelope helpers, JSON/CSV/table output
- [x] `onefinance/cli/app.py` — Typer app with all 7 data commands
- [x] `onefinance/cli/__main__.py` — `python -m onefinance.cli` entry point
- [x] JSON-first output, `--format table|csv`
- [x] Structured error JSON on failures
- [x] Exit codes 0/1/2/3/4
- [x] `--no-cache`, `--provider`, `--ttl` per-call overrides

## M10 — CLI: introspection, agent-help conventions ✅
- [x] `ofclient capabilities` — machine-readable manifest (schema_version, commands, arguments, examples)
- [x] `ofclient version` — package + schema version
- [x] `ofclient providers status` — per-provider cooldown/health state
- [x] `ofclient cache stats` — entry count, size, hit/miss counts
- [x] `ofclient config show` — active tiers, cache, cooldown settings
- [x] `ofclient config init` — generate config.yaml template
- [x] `--dry-run` on all 7 data commands (cache key + would_fetch preview)
- [x] Help text conventions (DESCRIPTION, WHEN TO USE, WHEN NOT TO USE, EXAMPLES)
- [x] **Total: 266 unit tests passing**
