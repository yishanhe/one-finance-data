# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This project uses **[uv](https://docs.astral.sh/uv/)** for dependency management with `hatchling` as the build backend. `uv.lock` is committed for reproducible installs.

```bash
# Install published package (includes ofclient CLI)
pip install onefinance

# On externally-managed Python (e.g. Homebrew), pick one:
uv tool install onefinance                          # recommended: isolated env, ofclient on PATH
uv tool upgrade onefinance                          # upgrade later
pipx install onefinance                             # alternative: same idea, uses pipx
pip3 install onefinance --break-system-packages     # alternative: installs into system Python

# Setup for development — install all deps in one shot
uv sync --all-extras

# Or install only what you need
uv sync                  # core deps only

# Run all tests
uv run pytest tests/

# Run a single test file
uv run pytest tests/unit/test_client.py

# Run a single test
uv run pytest tests/unit/test_client.py::TestOneFinanceClient::test_price_history_caching -v

# Exclude integration (live-network) tests
uv run pytest tests/ -m "not integration"

# Lint / format / type-check
uv run ruff check onefinance tests
uv run ruff format onefinance tests
uv run mypy onefinance tests

# Add a dependency
uv add <package>                    # runtime dep
uv add --dev <package>              # dev-only dep
uv add --optional cli <package>     # CLI extra dep

# Run the CLI (ofclient)
uv run ofclient --help
uv run ofclient quote AAPL
uv run ofclient price AAPL --range 1y
uv run ofclient indicators AAPL
uv run ofclient indicators AAPL --range 1y
uv run ofclient news AAPL
uv run ofclient actions AAPL
uv run ofclient holders AAPL
uv run ofclient analyst AAPL
uv run ofclient capabilities
uv run ofclient providers check          # validate API keys + tier setup
uv run ofclient providers check --ping   # also call each provider to verify liveness
uv run ofclient audit stats --format table
uv run ofclient audit recent --limit 10 --format table
uv run ofclient audit path

# Build distributable (sdist + wheel)
uv build
```

**Python 3.11+ required** (pinned to 3.13 in `.python-version`). Integration tests (marked `@pytest.mark.integration`) hit live APIs and are excluded from CI runs.

## Releasing to PyPI

Publishing is automated by `.github/workflows/publish.yml`. The workflow triggers on any tag matching `v*`, builds the sdist + wheel, runs `tests/smoke_test.py` against both artifacts in isolated envs, and uploads to PyPI via **trusted publishing (OIDC)** — no API tokens are stored in the repo.

### Version is derived from the git tag

The git tag is the single source of truth for the package version. `pyproject.toml` declares `version` as `dynamic`, and `hatch-vcs` reads the latest reachable `v*` tag at build time to set both the wheel/sdist filename and the generated `onefinance/_version.py` (which `onefinance/__init__.py` re-exports as `__version__`). There is no static version string anywhere in the repo to keep in sync.

Between releases, `uv build` produces a dev version like `0.1.4.dev3+g<sha>` (PEP 440), and `uv build` against a dirty working tree appends `.dYYYYMMDD`. The publish workflow's "Verify built version matches tag" step refuses to upload anything that doesn't match `${GITHUB_REF_NAME#v}` exactly — so a dirty tree or missing tag at release time fails fast before touching PyPI.

### Cutting a release

```bash
# 1. Make sure the working tree is clean and on main with everything you want shipped.
git status   # must be clean
git pull --rebase

# 2. (Optional) Dry-run the build + smoke tests locally. Between releases the
#    version will be a dev string — that's expected; CI is what enforces a clean tag.
rm -rf dist && uv build
uv run --isolated --no-project --with dist/onefinance-*-py3-none-any.whl tests/smoke_test.py
uv run --isolated --no-project --with dist/onefinance-*.tar.gz          tests/smoke_test.py

# 3. Tag and push. The tag push is what triggers the workflow.
git tag vX.Y.Z
git push origin vX.Y.Z

# 4. Watch the run: gh run watch  (or check the Actions tab on GitHub).
```

If the workflow fails after the tag has been pushed, fix forward with a new patch tag (e.g. `v0.1.5`) — never delete and re-push the same tag, since PyPI rejects re-uploads of an existing version.

### One-time setup (already done; documented here for recovery)

- **PyPI trusted publisher** at <https://pypi.org/manage/project/onefinance/settings/publishing/>:
  `owner = yishanhe`, `repo = one-finance-data`, `workflow = publish.yml`, `environment = pypi`.
- **GitHub environment** named `pypi` under repo Settings → Environments. Restrict it to tag pushes; optionally require reviewers.

No `UV_PUBLISH_TOKEN` secret is needed in CI — trusted publishing supersedes it. The env var remains useful for ad-hoc `uv publish` from a developer machine.

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
- **Type A** (historical, long TTL): `get_price_history`, `get_info`, `get_financials`, `get_insider_trades`, `get_news`, `get_corporate_actions`, `get_institutional_holders`, `get_analyst_data`
- **Type B** (always-current, 30 s): `get_quote`
- **Type C** (caller-controlled via `fresh` flag): `get_ratios`, `get_earnings`

### Providers (`onefinance/providers/`)

`BaseProvider` is an ABC with 7 endpoint stubs that each raise `NotSupportedError`. Subclasses override only what they support. The `supports(endpoint)` method uses reflection to check whether a method has been overridden — the client uses this for capability discovery.

Concrete providers:
- **`YFinanceProvider`** — free, unofficial Yahoo scraper; supports `get_price_history` + `get_info`; fragile, used as last resort
- **`FMPProvider`** — stable REST API (Financial Modeling Prep); supports most endpoints; requires `FMP_API_KEY` env var; free tier = 250 calls/day

M5 will replace the linear provider walk with a tier-walking router that tracks per-provider cooldowns.

### CacheManager (`onefinance/cache/manager.py`)

Wraps `diskcache` (SQLite-backed, default at `~/.one_finance_data/cache`, 2 GB LRU). Stores models as JSON envelopes with a `__type__` field for registry-based deserialization.

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
| `UV_PUBLISH_TOKEN` | PyPI token for `uv publish`; exported in shell env |


## Provider Capability Parity
All core alternative data (news, corporate actions, institutional holders, analyst data) as well as advanced endpoints (options chains, market screeners, and sector overviews) have been thoroughly integrated across the platform. While `yfinance` and `fmp` natively power many of these complex endpoints, we have structurally expanded `finnhub` and `twelve_data` to ensure uniform coverage. 
- **FMP**: Natively supports deep financial metrics, alternative data, and screeners.
  - API Documentation: [https://site.financialmodelingprep.com/developer/docs](https://site.financialmodelingprep.com/developer/docs)
- **Finnhub**: Extended to support `news` and `analyst_data`. Unsupported endpoints safely fallback via `NotSupportedError`.
  - Python SDK: [https://github.com/Finnhub-Stock-API/finnhub-python](https://github.com/Finnhub-Stock-API/finnhub-python)
  - API Documentation: [https://finnhub.io/docs/api](https://finnhub.io/docs/api)
- **Twelve Data**: Intraday `interval` mapping implemented natively. Unsupported alternative endpoints leverage the router's fallback logic.
  - Python SDK: [https://github.com/twelvedata/twelvedata-python](https://github.com/twelvedata/twelvedata-python)
  - API Documentation: [https://twelvedata.com/docs](https://twelvedata.com/docs)
- **Intraday Granularity**: Fully supported across *all* providers via standardized `interval` mapping (e.g. `1m`, `5m`, `1h`) and timestamp-aware `PriceBar` models.

## Development Guidelines

- **Scratch Scripts**: Please place any experimental or one-off scratch scripts (e.g., API testing) into the `scripts/` directory to keep the root of the project organized.
