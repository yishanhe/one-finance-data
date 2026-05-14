# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev,cli]"

# Run all tests
python -m pytest tests/

# Run a single test file
python -m pytest tests/unit/test_client.py

# Run a single test
python -m pytest tests/unit/test_client.py::TestOneFinanceClient::test_price_history_caching -v

# Exclude integration (live-network) tests
python -m pytest tests/ -m "not integration"

# Lint / format / type-check
ruff check onefinance tests
ruff format onefinance tests
mypy onefinance tests
```

**Python 3.11+ required.** Integration tests (marked `@pytest.mark.integration`) hit live APIs and are excluded from CI runs.

## Architecture

`onefinance` is a unified financial data client that abstracts multiple providers behind a single interface. The stack is three layers deep:

```
OneFinanceClient  →  CacheManager  →  BaseProvider subclasses
```

### OneFinanceClient (`onefinance/core/client.py`)

The public API. Exposes 7 endpoint methods that all funnel through `_cached_fetch`:

1. Check cache (skip if `no_cache=True`)
2. Try each configured provider in order; skip silently on `NotSupportedError`, collect other errors
3. Cache successful result with endpoint-appropriate TTL
4. Raise `AllProvidersFailedError` if every provider fails

Per-call overrides: `no_cache`, `provider` (force a specific provider by name), `ttl`.

Endpoint TTL types:
- **Type A** (historical, long TTL): `get_price_history`, `get_info`, `get_financials`, `get_insider_trades`
- **Type B** (always-current, 30 s): `get_quote`
- **Type C** (caller-controlled via `fresh` flag): `get_ratios`, `get_earnings`

### Providers (`onefinance/providers/`)

`BaseProvider` is an ABC with 7 endpoint stubs that each raise `NotSupportedError`. Subclasses override only what they support. The `supports(endpoint)` method uses reflection to check whether a method has been overridden — the client uses this for capability discovery.

Concrete providers:
- **`YFinanceProvider`** — free, unofficial Yahoo scraper; supports `get_price_history` + `get_info`; fragile, used as last resort
- **`FMPProvider`** — stable REST API (Financial Modeling Prep); supports most endpoints; requires `FMP_API_KEY` env var; free tier = 250 calls/day

M5 will replace the linear provider walk with a tier-walking router that tracks per-provider cooldowns.

### CacheManager (`onefinance/cache/manager.py`)

Wraps `diskcache` (SQLite-backed, default at `~/.finance_cache`, 2 GB LRU). Stores models as JSON envelopes with a `__type__` field for registry-based deserialization.

TTL logic:
- Quotes: 30 s
- Financials: 7 days; info: 30 days; insider trades: 1 day
- Ratios/earnings: 7 days default, 1 hour when `fresh=True`
- Price history: 30 days (fully historical), 1 minute (today's bar still forming), 6 hours (market closed)

### Cache Keys (`onefinance/cache/keys.py`)

`make_key(data_type, **params)` → `"{data_type}:{sha256[:16]}"`. Parameters are JSON-serialized with `sort_keys=True` and dates normalized to ISO strings, so the same `(symbol, date_range)` from any provider maps to one cache entry.

### Models (`onefinance/core/models.py`)

Nine frozen Pydantic models (`frozen=True`, `extra="forbid"`), all inheriting from `FinanceModel`:
`PriceBar`, `Quote`, `IncomeStatement`, `BalanceSheet`, `CashFlow`, `CompanyInfo`, `FinancialRatios`, `EarningsRecord`, `InsiderTrade`.

Every model carries `source` (provider name) and `fetched_at` (UTC timestamp). Custom annotated types (`Symbol`, `Currency`) enforce value constraints.

### Errors (`onefinance/core/errors.py`)

All exceptions inherit from `FinanceError`, which carries:
- `code` — stable string identifier (e.g., `PROVIDER_QUOTA_EXHAUSTED`) for agent branching
- `retry_safe` — whether a retry is likely to succeed
- `retry_after_seconds` — cooldown hint for rate-limit errors
- `to_dict()` — JSON serialization for CLI output

Key subclasses: `ProviderError`, `NotSupportedError`, `RateLimitError`, `AllProvidersFailedError`, `InvalidArgumentError`, `ConfigError`.

## Environment Variables

| Variable | Purpose |
|---|---|
| `FMP_API_KEY` | Required when using `FMPProvider` |
| `FINNHUB_API_KEY` | Required when using `FinnhubProvider` |
| `TWELVE_DATA_API_KEY` | Required when using `TwelveDataProvider` |

## Milestone State

| Milestone | Status |
|---|---|
| M1 — models + BaseProvider + YFinanceProvider | Complete |
| M2 — CacheManager | Complete |
| M3 — OneFinanceClient skeleton | Complete |
| M4 — FMPProvider | Complete |
| M5 — ProviderRouter + config | Complete |
| M6 — FinnhubProvider (all 7 endpoints) | Complete |
| M7 — TwelveDataProvider (price history + quote) | Complete |
| M8 — Telemetry/README/notebook | Skipped (telemetry); README done |
| M9 — CLI data commands | Complete |
| M10 — CLI introspection + --dry-run | Complete |
