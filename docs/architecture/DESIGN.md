# OneFinanceClient — Design Doc

**Status:** Draft v2 (tiered provider rewrite)
**Author:** Shanhe
**Last updated:** 2026-05-13

---

## 1. Background & motivation

Financial research workflows pull data from multiple providers. Each provider has its own SDK quirks, response schemas, rate limits, and cost structure. Three problems result:

1. **Schema friction.** Every notebook re-implements normalization from `yfinance`, `fmpsdk`, Finnhub etc. raw responses. Schema drift silently breaks downstream code.
2. **API cost & quota.** FMP charges per call and has a daily cap. Other free-tier providers (Twelve Data 800/day, Polygon 5/min) have even tighter limits. Identical requests re-issued across sessions burn quota fast.
3. **Single point of failure.** yfinance breaks regularly (it's an unofficial scraper). FMP can hit daily limits. A workflow that hard-depends on one provider stops when that provider stops.

This project builds a Python library that:

- **(a)** presents a single uniform API across providers (`get_price`, `get_financials`, etc.)
- **(b)** uses **tiered provider redundancy** — multiple providers cover the same endpoints, ranked by quality; lower tiers serve as fallback when higher tiers hit rate limits or fail
- **(c)** transparently caches responses on disk to eliminate redundant calls
- **(d)** classifies endpoints by their natural freshness model (see §6) so high-value provider quota is spent only on endpoints that genuinely need current data — historical data always goes through cache, current data always goes through realtime-capable providers, and a small set of caller-decided endpoints expose a `fresh` argument
- **(e)** ships a CLI (`ofclient`) **explicitly designed for AI agents to consume** — structured JSON I/O, self-describing help, structured errors, introspection commands. Human users are a secondary audience; the primary one is LLM-driven workflows where the agent has no access to external documentation and must figure out the tool from the tool itself (see §16)

Non-goals: real-time streaming, order execution, server deployment, multi-user concurrency.

---

## 2. Goals

| Goal | Success criterion |
|---|---|
| Uniform API across providers | Caller code is identical regardless of which provider serves the request |
| Tiered redundancy | When a higher-tier provider fails or rate-limits, lower tiers transparently take over |
| Smart quota usage | Premium provider (FMP) quota only spent on endpoints whose freshness model requires it (Type B always, Type C with `fresh=True`, or any endpoint when higher tiers exhaust) |
| Reduce API cost | >80% cache hit rate on typical research workflows |
| Easy to add providers | Adding a new provider = one new file implementing `BaseProvider`, plus a config line |
| Type-safe responses | All return values are validated Pydantic models, not raw dicts |
| Agent-friendly CLI | An LLM can use `ofclient` correctly from `--help` output alone, without external documentation (see §16) |

---

## 3. Scope

Seven data endpoints in v1. Each is classified by its **freshness model** (see §6 for the full taxonomy):

| Endpoint | Freshness | Providers (highest → lowest tier) |
|---|---|---|
| `get_price_history` | Type A — historical | FMP, Finnhub, Twelve Data, yfinance |
| `get_quote` | Type B — always current | FMP, Finnhub, yfinance |
| `get_financials` | Type A — historical | FMP, Finnhub, yfinance |
| `get_info` | Type A — historical | FMP, Finnhub, yfinance |
| `get_insider_trades` | Type A — historical | FMP, Finnhub |
| `get_ratios` | Type C — caller decides | FMP, Finnhub |
| `get_earnings` | Type C — caller decides | FMP, Finnhub |

Tier order is a config decision (see §6), not hardcoded.

---

## 4. Project structure

Single monorepo, single pip-installable package, with the CLI as an optional extra.

### Package layout

```
onefinance/
├── __init__.py              re-exports OneFinanceClient, error types
├── core/
│   ├── client.py            OneFinanceClient (public API)
│   ├── router.py            tier walking, cooldown logic
│   ├── models.py            Pydantic models (PriceBar, Quote, etc.)
│   ├── errors.py            FinanceError hierarchy
│   └── config.py            YAML loader, defaults
├── providers/
│   ├── base.py              BaseProvider ABC
│   ├── fmp.py
│   ├── finnhub.py
│   ├── twelve_data.py
│   └── yfinance.py
├── cache/
│   ├── manager.py           CacheManager (diskcache wrapper)
│   └── keys.py              make_key, hash helpers
└── cli/
    ├── __init__.py
    ├── __main__.py          enables `python -m onefinance.cli`
    ├── app.py               Typer app, command definitions
    └── format.py            Rich tables, JSON output formatting

tests/
├── unit/
├── integration/             hit real APIs (gated by env var)
└── fixtures/                recorded responses for provider adapters

pyproject.toml
README.md
```

### `pyproject.toml` essentials

```toml
[project]
name = "onefinance"
version = "0.1.0"
dependencies = [
    "diskcache>=5.6",
    "pydantic>=2.0",
    "pyyaml",
    "httpx",
    "yfinance",
]

[project.optional-dependencies]
cli = ["typer>=0.12", "rich"]
dev = ["pytest", "pytest-asyncio", "mypy", "ruff"]

[project.scripts]
ofclient = "onefinance.cli.app:app"
```

Three install modes:

| Command | What you get |
|---|---|
| `pip install onefinance` | Core library only (lightweight) |
| `pip install onefinance[cli]` | Adds `ofclient` command |
| `pip install onefinance[cli,dev]` | Adds test/lint tooling |

### Why this layout

**Single package, not two.** CLI lives under `onefinance.cli` rather than a separate `ofclient` package. One git repo, one version, one changelog — CLI and library can never drift out of sync.

**CLI is an extra dependency group.** Library users don't get Typer + Rich forced on them. Most users want `from onefinance import OneFinanceClient` and nothing else.

**`core/` subdirectory.** Keeps the top-level namespace tight — four subdirs (`core`, `providers`, `cache`, `cli`) communicate the architecture at a glance. The `__init__.py` re-exports the common types so callers write `from onefinance import OneFinanceClient` rather than the deeper path.

**`cli/__main__.py`.** Lets `python -m onefinance.cli ...` work even when the entry-point script isn't on PATH. Important for CI, Docker, and isolated virtualenvs.

**CLI contains zero business logic.** `cli/app.py` parses arguments, calls `OneFinanceClient`, and renders output via `format.py`. All real logic lives in `core/`. Consequence: adding a new provider or endpoint to the library automatically works from the CLI with no CLI changes needed.

### CLI command surface (read-only)

```bash
# Introspection — how an agent discovers what's available
ofclient capabilities                         # list all commands + schemas (JSON)
ofclient version                              # version + schema_version

# Configuration
ofclient config init                          # generate ~/.onefinance/config.yaml template
ofclient config show                          # print current config

# Data fetching — Type A (historical, no freshness arg)
ofclient price AAPL                           # most recent trading day
ofclient price AAPL --range 1y                # past year
ofclient price AAPL --start 2024-01-01
ofclient financials AAPL --statement income --period annual
ofclient info AAPL
ofclient insiders AAPL --since 2024-01-01

# Data fetching — Type B (always current)
ofclient quote AAPL

# Data fetching — Type C (caller decides via --fresh)
ofclient ratios AAPL --period annual
ofclient ratios AAPL --period annual --fresh
ofclient earnings AAPL
ofclient earnings AAPL --fresh

# State inspection — agent's working memory
ofclient cache stats                          # entries, size, hit rate
ofclient cache show <key>                     # inspect a cached entry
ofclient providers status                     # which providers are in cooldown
ofclient telemetry --last 7d                  # call stats over a window
ofclient log --last 20                        # recent invocations

# Output format
ofclient price AAPL                           # default JSON (see §16 — agent-first)
ofclient price AAPL --format table            # human-readable Rich table
ofclient price AAPL --format csv > prices.csv

# Universal flags (any data-fetching command)
--no-cache                                    # bypass cache entirely
--provider <name>                             # force a specific provider
--ttl <seconds>                               # override library's TTL choice
--dry-run                                     # show what would happen, no fetch
```

**No `cache clear`, no `cache invalidate`, no config mutation beyond `init`.** Destructive operations require the Python API — keeps the CLI footprint small and avoids both "oops" moments in shell history and agent confusion about which commands have side effects.

**Why JSON is the default output**, not table: this CLI is primarily for AI agents (see §16). Humans get pretty tables via `--format table`. This inversion vs traditional CLIs is intentional.

---

## 5. High-level architecture

Five layers:

1. **Caller** — user code (notebooks, backtest scripts, dashboards)
2. **`OneFinanceClient`** — the public API; pure functions like `get_price()`, `get_financials()`
3. **Cache manager** — diskcache-backed, TTL-aware
4. **Provider router** — picks provider tier based on capability, caller intent, and quota state
5. **Providers** — adapters wrapping FMP / Finnhub / Twelve Data / yfinance

### Request flow

```
caller → OneFinanceClient.get_X(symbol, ...)
       → CacheManager.get(key)         ── hit ──→ return cached
                                        └─ miss ─→ Router.select_provider(endpoint, freshness_type)
                                                  ├─ try Provider[0] (highest tier)
                                                  │   ├─ success → cache.set() → return
                                                  │   ├─ RateLimitError → mark cooldown, try next
                                                  │   ├─ NotSupportedError → try next
                                                  │   └─ ProviderError → log, try next
                                                  └─ all exhausted → raise AllProvidersFailedError
```

Cache sits on the normalized side — providers return uniform Pydantic models, the cache stores their JSON serialization. Swapping providers does not invalidate cache.

---

## 6. Provider tier strategy

Providers are not specialized by endpoint — they are **ranked redundancy** for the same endpoints.

### Freshness taxonomy

Before discussing tiers, we classify each endpoint by its **freshness model**. This determines whether the caller has any choice about cache vs fresh fetch, and how the router selects providers.

**Type A — historical / slow-changing.** The underlying data updates rarely or never. Examples: closed-day price bars, quarterly financial statements, company info, filed insider trades. There is no meaningful "fresher" version available within a normal cache window, so the API does **not** expose a freshness argument — calls always go through cache with a long TTL. Knowledge built into the library, not the caller.

**Type B — always current.** The data is only meaningful "right now." Examples: realtime quote, current intraday state. The method name itself signals intent (`get_quote`), and the library always treats these as fresh-needed with very short TTL (30s). No argument — the semantics are baked into the endpoint.

**Type C — caller decides.** The data has a slowly-moving "official" version and a frequently-updating "live" version, and which one matters depends on the use case. Examples: financial ratios (research vs alert system), earnings estimates (backtest vs live trading). These endpoints take a `fresh: bool = False` argument.

| Type | Argument | TTL behavior | Provider tier |
|---|---|---|---|
| A | none | Long, auto-derived | Single list, free-tier-first |
| B | none | Short (30s) | Single list, realtime-capable first |
| C | `fresh=False` (default) | Long | Free-tier-first |
| C | `fresh=True` | Short | Premium-first |

### Why `fresh` and not `realtime`

In finance, `realtime` has a specific connotation — streaming tick data, low-latency, exchange-licensed feeds. This library does not provide that. Using `realtime` would imply guarantees the system can't keep. `fresh` accurately describes what the flag does: bypass the long-TTL cache, fetch the most recent available version from a provider, and accept the latency / quota cost.

### Endpoint freshness classification

| Endpoint | Type | Reasoning |
|---|---|---|
| `get_price_history(symbol, start, end)` | A | History doesn't change. TTL derived from `end` date — see §10 |
| `get_quote(symbol)` | B | Quote without "now" has no meaning |
| `get_financials(symbol, statement, period)` | A | Quarterly cadence; reissues are rare and tracked separately |
| `get_info(symbol)` | A | Company name / sector rarely changes |
| `get_insider_trades(symbol, since=None)` | A | Form 4 filings are append-only |
| `get_ratios(symbol, period, fresh=False)` | C | Computed from financials; estimate-driven ratios update intraday |
| `get_earnings(symbol, fresh=False)` | C | Reported earnings are A-like; estimates revise constantly |

### Method-name-first principle

Whenever possible, freshness intent is expressed in the **method name** rather than an argument. `get_price_history(...)` and `get_quote(...)` are different methods precisely because they have different freshness semantics; a single `get_price(..., realtime=...)` would be a worse design. Arguments are the last resort, reserved for endpoints where the same logical data legitimately has both interpretations (Type C).

### Tier ranking criteria

For Types A and B, each endpoint has one tier list. For Type C, two lists (`default` and `fresh`). Ordering considers:

1. **Data quality** — FMP and Finnhub have cleanest, most consistent data for fundamentals
2. **Recency** — for Type B and Type C `fresh=True`, prefer providers with realtime feeds (FMP paid, Finnhub realtime US)
3. **Quota economics** — burn the most-restricted quota last; Twelve Data's 8/min is tighter than Finnhub's 60/min, so Finnhub goes first when both are eligible
4. **Reliability** — yfinance is unofficial and breaks regularly; always last resort

### Free-tier provider survey (as of 2026)

| Provider | Free-tier limit | Realtime US equities | Fundamentals | Notes |
|---|---|---|---|---|
| FMP | 250/day (free); paid plans uncapped | ✓ (paid) | ✓ (deep) | Primary paid provider; best fundamentals |
| Finnhub | 60/min | ✓ (some 20-min delay on free) | ✓ (partial on free) | Most generous free tier |
| Twelve Data | 800/day, 8/min | partial | partial | Good for global; tight per-minute |
| yfinance | unofficial / unlimited | near-realtime | partial | Always last; can break without notice |

### Routing config

The router consults a config-driven tier table. Type A and B endpoints have a single list; Type C endpoints have two:

```yaml
tiers:
  # Type A — historical, cache-heavy, free-tier-first
  price_history:  [fmp, finnhub, twelve_data, yfinance]
  financials:     [fmp, finnhub, yfinance]
  info:           [fmp, finnhub, yfinance]
  insider_trades: [fmp, finnhub]

  # Type B — always current, prefer realtime-capable providers
  quote: [fmp, finnhub, yfinance]

  # Type C — caller decides via fresh=
  ratios:
    default: [fmp, finnhub]
    fresh:   [fmp, finnhub]
  earnings:
    default: [fmp, finnhub]
    fresh:   [fmp, finnhub]
```

> **Observation.** Today, Type C's `default` and `fresh` tier lists for `ratios` and `earnings` are identical — because only FMP and Finnhub support those endpoints. The difference between `fresh=True` and `fresh=False` therefore mainly affects **TTL**, not provider selection. The two-list structure exists for when future providers (Tiingo, EODHD, etc.) are added and the choice becomes meaningful.

On each call the router walks the relevant tier list, skipping providers currently in cooldown (see §7), and returns the first one that succeeds. The caller never sees which provider was actually used unless they inspect `result.source`.

---

## 7. Quota & rate-limit handling

Rather than predict quota state with a local counter, the system **reacts to provider responses**. Quota tracking is passive and provider-driven.

### Cooldown mechanism

When a provider returns a rate-limit signal, the router marks it in cooldown for a backoff window. While in cooldown, the router skips it and tries the next tier.

```python
class ProviderState:
    name: str
    cooldown_until: float       # unix timestamp; 0 = available
    last_error: str | None
    consecutive_failures: int
```

### Detecting rate limits

Each provider adapter inspects the response and classifies failures:

| Signal | Provider | Cooldown |
|---|---|---|
| HTTP 429 | All | min(60s × 2^failures, 1h) |
| FMP error: `"Limit Reach"` in body | FMP | 1h (likely daily cap) |
| Finnhub HTTP 429 with `Retry-After` header | Finnhub | header value |
| Twelve Data `"code": 429` in body | Twelve Data | 60s |
| yfinance empty response / `YFRateLimitError` | yfinance | 5min |

Each adapter exposes `is_rate_limited(response) -> bool` and `cooldown_for(response) -> float` so router logic stays generic.

### State persistence

Provider state is kept in-memory only — does not persist across process restarts. Rationale: cooldowns are short (≤1h), and a fresh process has no incentive to remember stale failures. If a process runs continuously (notebook session, dashboard), cooldown state accumulates and works.

Exception: if a provider hits its *daily* cap, cooldown is 1 hour and gets re-checked. After the cooldown the router will retry once; if rate-limited again, cooldown re-extends. This naturally throttles retries without persistent state.

### Telemetry

Every dispatch logs `(endpoint, chosen_provider, cache_hit, latency_ms, error)`. Stored in diskcache under a separate `telemetry` namespace; queryable via `client.telemetry.summary()`. This lets the user see things like:

- "Last 7 days: 84% cache hit; FMP served 12% of misses, Finnhub 3%, yfinance 1%."
- "FMP hit cooldown 4 times yesterday."

---

## 8. Data model

All responses are Pydantic v2 `BaseModel` subclasses in `models.py`, configured as **frozen** and **extra-forbidden**:

- `frozen=True` — instances are immutable (safe to share, hashable, can be used as dict keys / set members)
- `extra="forbid"` — reject unknown fields at validation time so provider schema drift fails loudly rather than silently dropping data

Pydantic chosen over plain `@dataclass` because:

1. **Validation at the boundary** — each provider adapter feeds raw API responses through the model; bad types (string instead of float, missing required field) raise immediately instead of poisoning the cache
2. **JSON serialization is free** — `model.model_dump_json()` for diskcache storage, `Model.model_validate_json()` to deserialize
3. **Constraint expression** — share counts must be `>= 0`, currency codes must be 3 letters, etc., live next to the type
4. **Better IDE experience** — auto-complete, type checking, mypy-friendly

```python
from datetime import date, datetime
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field

class FinanceModel(BaseModel):
    """Shared config for all finance data models."""
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

Symbol = Annotated[str, Field(min_length=1, max_length=20, pattern=r"^[A-Z0-9.\-]+$")]
Currency = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]

class PriceBar(FinanceModel):
    symbol: Symbol
    date: date
    open: Annotated[float, Field(ge=0)]
    high: Annotated[float, Field(ge=0)]
    low: Annotated[float, Field(ge=0)]
    close: Annotated[float, Field(ge=0)]
    adj_close: Annotated[float, Field(ge=0)]
    volume: Annotated[int, Field(ge=0)]
    source: str            # which provider supplied this
    fetched_at: datetime   # when this record was fetched (UTC)

class Quote(FinanceModel):
    symbol: Symbol
    timestamp: datetime    # quote time as reported by provider
    price: Annotated[float, Field(ge=0)]
    bid: Annotated[float, Field(ge=0)] | None = None
    ask: Annotated[float, Field(ge=0)] | None = None
    volume: Annotated[int, Field(ge=0)]
    source: str
    fetched_at: datetime   # when we fetched it (UTC)

class IncomeStatement(FinanceModel):
    symbol: Symbol
    period: str            # "2024-Q3" or "2024-FY"
    fiscal_date: date
    revenue: float
    cost_of_revenue: float
    gross_profit: float
    operating_income: float
    net_income: float
    eps_basic: float
    eps_diluted: float
    currency: Currency
    source: str
    fetched_at: datetime

# Similar for BalanceSheet, CashFlow, CompanyInfo, FinancialRatios,
# EarningsRecord, InsiderTrade — all subclass FinanceModel and carry
# `source` + `fetched_at` so caller can audit which provider answered and when.
```

Note that financial-statement fields (revenue, net_income, etc.) are **not** constrained to `>= 0` — companies legitimately report negative revenue (refunds exceeding sales in a small segment), negative gross profit, etc. Validation is permissive for accounting fields, strict for price/volume.

`source` + `fetched_at` together give a full audit trail: which provider answered, and when. Useful for:

- Spotting stale data ("this `fetched_at` is from 3 days ago — is it still valid?")
- Debugging cache behavior without poking into diskcache internals
- Reproducing a backtest against the exact snapshot of data that was current at the time

`fetched_at` is set inside the provider adapter at the moment the response is normalized — not by the cache layer. This way the timestamp survives a cache round-trip unchanged: when a value comes back from disk, `fetched_at` still reflects when it was *originally* fetched from the provider, not when it was last read from cache.

### Cache serialization

Pydantic's `model_dump_json()` produces deterministic JSON suitable for diskcache. The cache stores a small envelope: `{"type": "PriceBar", "data": <model_json>}` so the cache layer can dispatch to the right `model_validate_json()` call on read. This means the cache stores text, not pickle bytes — debuggable with `cat` if needed.

### Provider adapter integration

Each provider's `_normalize(raw_response) -> Model` method calls `Model.model_validate({...})`. Validation errors during normalization are wrapped in `ProviderError` (with the underlying `ValidationError` as `__cause__`) and trigger fallback to the next tier — the router treats schema drift the same as any other provider failure.

Functions return `list[PriceBar]`, `list[IncomeStatement]`, etc. Callers who want a DataFrame can do `pd.DataFrame([b.model_dump() for b in bars])` — pandas is not a required dependency.

---

## 9. Provider interface

```python
class BaseProvider(ABC):
    name: str

    @abstractmethod
    def get_price_history(self, symbol: str, start: date, end: date,
                          interval: str = "1d") -> list[PriceBar]: ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def get_financials(self, symbol: str, statement: str,
                       period: str) -> list[IncomeStatement | BalanceSheet | CashFlow]: ...

    @abstractmethod
    def get_info(self, symbol: str) -> CompanyInfo: ...

    @abstractmethod
    def get_ratios(self, symbol: str, period: str) -> list[FinancialRatios]: ...

    @abstractmethod
    def get_earnings(self, symbol: str) -> list[EarningsRecord]: ...

    @abstractmethod
    def get_insider_trades(self, symbol: str,
                           since: date | None = None) -> list[InsiderTrade]: ...

    @abstractmethod
    def is_rate_limited(self, response: Any) -> bool: ...

    @abstractmethod
    def cooldown_for(self, response: Any) -> float:
        """Seconds to wait before retrying this provider."""
```

Each endpoint method has a default base implementation that raises `NotSupportedError` — providers override only what they actually support. Capability is discoverable via `provider.supports("ratios")`.

### Provider capability matrix (v1 target)

| Endpoint | FMP | Finnhub | Twelve Data | yfinance |
|---|---|---|---|---|
| `get_price_history` | ✓ | ✓ | ✓ | ✓ |
| `get_quote` | ✓ | ✓ (20m delay free) | partial | near-realtime |
| `get_financials` | ✓ | ✓ | ✗ | partial |
| `get_info` | ✓ | ✓ | ✗ | ✓ |
| `get_ratios` | ✓ | ✓ | ✗ | ✗ |
| `get_earnings` | ✓ | ✓ | ✗ | partial |
| `get_insider_trades` | ✓ | ✓ | ✗ | ✗ |

---

## 10. Cache layer

Backed by [diskcache](https://grantjenks.com/docs/diskcache/) — a SQLite-backed key-value store with a dict-like API.

### Key design

```python
key = f"{data_type}:{sha256(sorted_params)[:16]}"
# example: "price:a3f9c2e8b1d4f6a0"
```

The key does **not** include the provider name. A successful response from FMP and one from yfinance for the same `(symbol, date range)` are cached under the same key — whichever arrived first satisfies subsequent requests until TTL expiry. The `source` field on the returned model tells the caller which provider answered.

Params are JSON-serialized with `sort_keys=True` before hashing, so argument order doesn't cause misses.

### TTL by freshness type

TTLs follow the freshness taxonomy from §6 — the library picks the right one based on the endpoint and arguments, not the caller.

| Endpoint | Default TTL | Notes |
|---|---|---|
| `get_quote` (Type B) | 30s | Stale-but-useable within a minute |
| `get_price_history` (Type A) | **auto** | See "Smart TTL for price history" below |
| `get_financials` (Type A) | 7d | Quarterly cadence |
| `get_info` (Type A) | 30d | Rarely changes |
| `get_insider_trades` (Type A) | 1d | New filings daily; cache by `(symbol, since)` |
| `get_ratios(fresh=False)` (Type C) | 7d | Same cadence as underlying financials |
| `get_ratios(fresh=True)` (Type C) | 1h | Estimate-driven views may move |
| `get_earnings(fresh=False)` (Type C) | 7d | Reported actuals dominate |
| `get_earnings(fresh=True)` (Type C) | 1h | Analyst estimates revise intraday |

### Smart TTL for price history

`get_price_history` doesn't take a freshness argument — the appropriate TTL is fully derivable from the request. The cache manager picks:

```python
def ttl_for_price_history(start: date, end: date) -> int:
    today = date.today()
    if end < today:
        return 30 * 24 * 3600           # 30d — fully historical, near-immutable
    if is_market_open_now():
        return 60                       # 1min — today's bar still forming
    return 6 * 3600                     # 6h — market closed, bar settled
```

A request for `[2020-01-01, 2023-12-31]` caches for a month; a request for `[2020-01-01, today]` caches for 1 minute during the trading day and 6 hours after close. The caller writes one line; the library handles freshness.

`is_market_open_now()` checks US exchange hours (9:30–16:00 ET, weekdays, excluding NYSE holidays). v1 uses a hard-coded NYSE calendar baked at release time; future versions could pull from `pandas_market_calendars` if global markets are added.

### Tag-based invalidation

Every entry is tagged with its `data_type`. To wipe all financial-statement cache after discovering bad data:

```python
client.cache.invalidate_by_type("financials")
```

### Size management

diskcache enforces a `size_limit` (default 2 GB) and evicts least-recently-used entries when exceeded. No manual cleanup.

### `no_cache` escape hatch

Caller can bypass cache for a specific call: `client.get_quote("AAPL", no_cache=True)`. Useful for verifying live data without trashing the cache. Distinct from `fresh=True` on Type C endpoints: `fresh=True` still writes to cache (with a short TTL); `no_cache=True` reads and writes nothing.

---

## 11. Public API

```python
from onefinance import OneFinanceClient

client = OneFinanceClient(config="config.yaml")

# Type A — historical, no freshness argument
bars     = client.get_price_history("AAPL", start="2024-01-01", end="2024-12-31")
income   = client.get_financials("AAPL", statement="income", period="annual")
info     = client.get_info("AAPL")
trades   = client.get_insider_trades("AAPL", since="2024-01-01")

# Type B — always current, no argument needed
quote    = client.get_quote("AAPL")

# Type C — caller decides
ratios   = client.get_ratios("AAPL", period="annual")              # cached, free-tier-first
ratios   = client.get_ratios("AAPL", period="annual", fresh=True)  # short TTL, premium-first
earnings = client.get_earnings("AAPL")
earnings = client.get_earnings("AAPL", fresh=True)

# Per-call overrides (any endpoint)
bars = client.get_price_history("AAPL", ..., provider="finnhub")   # force a specific provider
bars = client.get_price_history("AAPL", ..., no_cache=True)        # bypass cache entirely
bars = client.get_price_history("AAPL", ..., ttl=3600)             # override library's TTL choice

# Diagnostics
client.cache.stats()                      # {"entries": 1247, "volume_mb": 84.3}
client.providers.state()                  # provider cooldown / health
client.telemetry.summary(last_days=7)     # cache hit %, provider mix
```

### API design principles

- **Method name expresses freshness intent.** `get_price_history` and `get_quote` are different methods because they mean different things — there is no `get_price(realtime=...)`.
- **Type C endpoints accept `fresh=False` (default).** Caller opts in to a premium fetch only when they actually need it.
- **`no_cache=True` is universal.** It works on any endpoint and means "ignore cache for both read and write." Useful for testing and one-off audits.
- **The returned model carries `source` and `fetched_at`.** Caller never has to guess which provider answered or how stale the data is.

---

## 12. Error handling

Errors are structured for both Python callers and CLI/agent consumers. Every exception carries a stable error code, a human-readable message, and — critically — guidance on what to do next.

```python
class FinanceError(Exception):
    code: str                          # stable identifier, e.g. "PROVIDER_QUOTA_EXHAUSTED"
    message: str                       # human-readable
    suggested_action: str | None       # what the caller (or agent) should do
    retry_safe: bool                   # is it worth retrying the same call?
    retry_after_seconds: int | None    # cooldown hint, if known

    def to_dict(self) -> dict:
        """Serializable form for CLI JSON output."""
        return {
            "code": self.code,
            "message": self.message,
            "suggested_action": self.suggested_action,
            "retry_safe": self.retry_safe,
            "retry_after_seconds": self.retry_after_seconds,
        }

class ProviderError(FinanceError): ...
class NotSupportedError(ProviderError): ...
class RateLimitError(ProviderError): ...
class AllProvidersFailedError(FinanceError):
    """All tiers exhausted for this endpoint."""
    failures: list[tuple[str, FinanceError]]   # (provider_name, error)
    fallback_providers_available: list[str]    # any providers still in cooldown that may recover
```

### Error code catalog

Stable identifiers an agent can branch on:

| Code | When raised | `retry_safe` |
|---|---|---|
| `SYMBOL_NOT_FOUND` | Ticker doesn't exist on any tried provider | false |
| `PROVIDER_QUOTA_EXHAUSTED` | Daily/per-minute limit hit, no fallback | true (after `retry_after_seconds`) |
| `PROVIDER_AUTH_FAILED` | Bad API key | false |
| `NOT_SUPPORTED` | Endpoint not implemented by any configured provider | false |
| `ALL_PROVIDERS_FAILED` | Every tier tried, all failed | true (after longest cooldown) |
| `INVALID_ARGUMENT` | Bad date range, unknown period, etc. | false |
| `CONFIG_MISSING_KEY` | Required API key not set in env | false |
| `NETWORK_ERROR` | Transient connectivity, all retries exhausted | true |
| `SCHEMA_DRIFT` | Provider returned data the model couldn't validate | true (next provider may have valid schema) |

### Behavior

- **`RateLimitError`** — provider goes into cooldown; router transparently tries next tier. Caller never sees this unless all tiers exhaust.
- **`NotSupportedError`** — try next tier silently.
- **Network / transient errors** — provider adapter retries with exponential backoff (max 3 attempts) internally. If still failing, router moves to next tier.
- **`AllProvidersFailedError`** — raised only when every tier has been tried. Includes per-provider failure detail so caller can diagnose; `suggested_action` summarizes the most recoverable path (e.g. "Wait 1h and retry; Finnhub becomes available at 14:25 UTC").
- **Missing data** (provider returns empty list legitimately) — return empty list, do not fall through. Empty is a valid answer.

Errors are **never cached.** Only successful responses go to disk.

### Example: agent-consumable error JSON

When the CLI catches a `FinanceError`, it serializes via `to_dict()` plus context:

```json
{
  "schema_version": "1.0",
  "status": "error",
  "error": {
    "code": "PROVIDER_QUOTA_EXHAUSTED",
    "message": "FMP daily quota exhausted (250/250 calls today)",
    "suggested_action": "Retry without --fresh (use cached data), or wait for daily reset at 00:00 UTC",
    "retry_safe": true,
    "retry_after_seconds": 14400,
    "fallback_providers_available": ["finnhub"]
  },
  "metadata": {
    "attempted_providers": [
      {"name": "fmp", "result": "quota_exhausted"}
    ]
  }
}
```

The agent can branch on `error.code`, decide based on `retry_safe`, and use `fallback_providers_available` to plan the next call.

---

## 13. Configuration

Everything routable lives in `config.yaml`:

```yaml
providers:
  fmp:
    api_key_env: FMP_API_KEY
    timeout_s: 10
  finnhub:
    api_key_env: FINNHUB_API_KEY
    timeout_s: 10
  twelve_data:
    api_key_env: TWELVE_DATA_API_KEY
    timeout_s: 10
  yfinance:
    timeout_s: 15

tiers:
  # See §6 for full table

cache:
  dir: ~/.finance_cache
  size_limit_gb: 2
  ttl_overrides:
    price_history_recent: 3600
    quote: 30

cooldown:
  default_initial_s: 60
  max_backoff_s: 3600
```

Tiers are user-editable. A heavy FMP user might move FMP to the bottom of Type A and Type C-`default` tier lists to preserve daily quota for Type B and Type C-`fresh` work.

---

## 14. Open questions & future work

**Cross-provider data validation.** When two free-tier providers can both answer, should the router cross-check? E.g. fetch from Finnhub, opportunistically compare to yfinance, flag discrepancies. Useful for catching bad data; defer to v2.

**Incremental price updates.** Currently a request for `[start, today]` re-fetches the whole range if `today` advances by one day (different cache key). Smarter: check cache for the longest overlapping range and only fetch the gap. Defer.

**Provider hot-swap on quality issues.** If FMP returns suspicious data (e.g. negative revenue, zero volume on a regular trading day), should the router automatically retry on Tier 2? Defer — requires sanity-check rules per endpoint.

**Persistent quota state.** Currently cooldowns reset on restart. For long-running services this is fine; for CLI tools invoked frequently it's wasteful. Could persist to a small JSON file. Defer.

**Currency normalization.** FMP and Finnhub sometimes report local currency, sometimes USD. Library does not normalize — too opinionated. Document clearly.

**Batch requests.** Discussed and intentionally deferred for v1. An agent typically issues one CLI call per reasoning step, so per-call overhead matters less than per-step interpretability. `client.batch([...])` and `ofclient batch` can be added later — partial success, concurrency limits per provider, JSONL streaming output, and dry-run quota planning are the open design questions. FMP and Finnhub support batch endpoints natively; others would need client-side fan-out.

---

## 15. Milestones

| Phase | Deliverable | Est. |
|---|---|---|
| M1 | `models.py` + `BaseProvider` + `YFinanceProvider` (price + info only) | 2 days |
| M2 | `CacheManager` + integration into M1 path | 1 day |
| M3 | `OneFinanceClient` skeleton with single-provider routing | 1 day |
| M4 | `FMPProvider` for all endpoints | 3 days |
| M5 | Router with tier walking + rate-limit detection + cooldowns | 2 days |
| M6 | `FinnhubProvider` (full coverage) | 2 days |
| M7 | `TwelveDataProvider` (partial coverage, price-focused) | 1 day |
| M8 | Telemetry, tests, README, example notebook | 3 days |
| M9 | CLI: data commands, JSON-first output, structured errors | 2 days |
| M10 | CLI: introspection (`capabilities`, `log`, `dry-run`), agent-help conventions | 2 days |

Total: ~3 weeks of focused work. M1–M3 is a functional MVP using only yfinance; M5 makes the system multi-provider; M6–M7 add redundancy; M9–M10 deliver the agent-facing CLI.

---

## 16. CLI design for agents

The `ofclient` CLI is the primary interface for AI agents (LLM-driven workflows) interacting with this library. Human users are a secondary audience. This section codifies the conventions that make the CLI agent-consumable.

### Why agents are the primary audience

An agent has fundamental constraints a human doesn't:

- It won't read your README. It depends on the CLI's own help/error output to figure out usage.
- It has limited working memory. Verbose, unstructured output wastes tokens and degrades downstream reasoning.
- It can't ask "what did you mean?" mid-execution. Every error must include enough information to recover or give up cleanly.
- It needs to plan multi-step workflows. Introspection commands let it check state before committing to expensive operations.

Designing for agents first happens to produce a better CLI for power users too. Designing for humans first produces a CLI agents struggle with.

### Output convention — JSON by default

Every command emits structured JSON on stdout by default. Pretty tables are opt-in via `--format table`. This inversion vs traditional CLIs is intentional.

Every successful response has the same envelope:

```json
{
  "schema_version": "1.0",
  "status": "success",
  "command": "price",
  "data": [
    {"symbol": "AAPL", "date": "2024-01-02", "close": 185.64, ...}
  ],
  "metadata": {
    "source": "fmp",
    "cache_hit": false,
    "fetched_at": "2026-05-13T14:23:01Z",
    "latency_ms": 240,
    "rows": 252
  }
}
```

Every error response uses the structure defined in §12:

```json
{
  "schema_version": "1.0",
  "status": "error",
  "command": "price",
  "error": { /* see §12 */ },
  "metadata": { "attempted_providers": [...] }
}
```

`schema_version` is bumped on breaking changes. Agents can pin to a known version with `--schema-version 1.0` (returns error if unsupported).

### Help text conventions

Each command's `--help` must include four sections, in order:

```
DESCRIPTION
  One-paragraph summary including what the command does and what
  freshness type it falls under (A/B/C, see §6).

WHEN TO USE
  Concrete scenarios where this command is the right choice.

WHEN NOT TO USE
  Adjacent scenarios with the right alternative command named.

ARGUMENTS
  Each argument lists:
  - name, type, required/optional
  - Allowed values: explicit enumeration (not free-form prose)
  - Default if any

EXAMPLES
  3-5 real invocations, with intent comments.

SEE ALSO
  Related commands the agent might chain with.
```

The "WHEN NOT TO USE" section is the one most often skipped in normal CLI docs and the most valuable for agents — it prevents adjacent-task confusion. Example for `ratios`:

```
WHEN NOT TO USE
  - For raw financial statements: use `ofclient financials` instead.
  - For trailing-12-month metrics on a specific date: use
    `ofclient financials --period ttm` then compute the ratio.
```

### Exit codes

Agents branch on exit codes more reliably than parsing stderr:

| Exit code | Meaning | Action |
|---|---|---|
| 0 | Success | Process the result |
| 1 | User error (bad args, unknown symbol) | Fix input, retry |
| 2 | Transient (rate limit, network) | Wait `retry_after_seconds`, retry |
| 3 | Permanent (endpoint not supported by any provider) | Give up |
| 4 | Configuration (missing API key) | Surface to user, don't retry |

The error JSON's `error.code` gives the precise reason; exit codes give the rough retry category. An agent can decide quickly using exit code alone, drill in via JSON for specifics.

### Introspection commands

Three commands exist so agents can plan without trial-and-error:

**`ofclient capabilities`** — Returns machine-readable manifest of every command, its arguments, allowed values, freshness type, and examples. An agent fetches this once at the start of a session and treats it as the source of truth.

```json
{
  "schema_version": "1.0",
  "commands": [
    {
      "name": "ratios",
      "description": "Fetch financial ratios for a symbol.",
      "freshness_type": "C",
      "arguments": [
        {"name": "symbol", "required": true, "type": "string", "pattern": "^[A-Z0-9.-]+$"},
        {"name": "period", "required": true, "type": "enum",
         "allowed": ["annual", "quarterly", "ttm"]},
        {"name": "fresh", "required": false, "type": "boolean", "default": false}
      ],
      "examples": [
        "ofclient ratios AAPL --period annual",
        "ofclient ratios AAPL --period quarterly --fresh"
      ]
    }
  ]
}
```

**`ofclient providers status`** — Returns current state of each configured provider (ok / cooldown / no_key) plus best-effort quota estimates. Agent checks this before issuing expensive `--fresh` calls.

**`ofclient cache stats`** + **`ofclient log`** — Cache hit rate and recent invocation history. Lets an agent inspect its own working memory across CLI calls.

### Dry-run

Any data-fetching command accepts `--dry-run`. Instead of executing, it returns the plan:

```json
{
  "status": "dry_run",
  "command": "price",
  "plan": {
    "would_fetch": false,
    "cache_hit_predicted": true,
    "cache_key": "price:a3f9c2e8b1d4f6a0",
    "cached_at": "2026-05-13T08:15:32Z",
    "ttl_remaining_s": 3600
  }
}
```

If the call would miss cache:

```json
{
  "status": "dry_run",
  "plan": {
    "would_fetch": true,
    "provider": "fmp",
    "fallback_chain": ["fmp", "finnhub", "yfinance"],
    "estimated_quota_cost": {"fmp": 1},
    "warnings": ["FMP has 8/250 daily quota remaining"]
  }
}
```

Agent uses this to decide whether to proceed, batch differently, or wait.

### No interactive prompts, ever

`ofclient config init` accepts `--from-env` (default), `--from-stdin`, or `--non-interactive` with `--set KEY=VALUE` pairs. It never blocks on `input()`. An agent that pipes nothing should still succeed (writing a template with placeholder values) rather than hang.

### Determinism and idempotence

Every read-only command is idempotent. The CLI guarantees:

- Same arguments + same cache state → identical output (modulo `latency_ms` and `fetched_at` on misses).
- `cache_hit: true` means zero provider calls happened. Agents safely retry without quota cost.
- No command modifies anything outside `~/.onefinance/cache/` and the configured config file. Nothing on PATH, in env, or in cwd.

### Environment overrides

For agent runners that prefer flags via env:

| Variable | Equivalent flag |
|---|---|
| `OFCLIENT_OUTPUT=json\|table\|csv` | `--format` |
| `OFCLIENT_NO_CACHE=1` | `--no-cache` |
| `OFCLIENT_DRY_RUN=1` | `--dry-run` |
| `OFCLIENT_CONFIG=/path/to/config.yaml` | `--config` |

Flags always win over env. This lets an agent set output mode once per session and forget about it.

---

## 17. Out of scope

- Real-time / WebSocket streaming
- Order execution or anything trading-related
- Cross-process or networked cache (single-user, single-machine)
- Provider-specific endpoints that don't fit the unified API (escape hatch: `client.raw(provider="fmp").some_specific_endpoint(...)`)
- pandas as a hard dependency
- Currency conversion or unit normalization
