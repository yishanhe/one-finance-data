# one-finance-data

Unified financial data client for Python. Abstracts FMP, Finnhub, Twelve Data, and Yahoo Finance behind a single interface with transparent disk-based caching and a CLI designed for agents and automation.

## Installation

```bash
pip install onefinance
```

The `ofclient` CLI is included. If you're on a system with an externally-managed Python (e.g. Homebrew), install as a tool instead:

```bash
uv tool install onefinance   # installs ofclient on your PATH
uv tool upgrade onefinance   # upgrade later
```

### Development install

```bash
git clone https://github.com/yishanhe/one-finance-data
cd one-finance-data
uv sync
```

## Environment variables

| Variable | Provider | Required |
|---|---|---|
| `FMP_API_KEY` | Financial Modeling Prep | For FMPProvider |
| `FINNHUB_API_KEY` | Finnhub | For FinnhubProvider |
| `TWELVE_DATA_API_KEY` | Twelve Data | For TwelveDataProvider |

Providers whose key is unset are skipped automatically. `YFinanceProvider` (no key) is always available as a fallback.

## Python usage

```python
from onefinance.core.client import OneFinanceClient
from datetime import date

# Reads API keys from environment; builds all available providers automatically
with OneFinanceClient() as client:
    # Price history — cached 30 days (historical) or 6 h (after market close)
    bars = client.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31))

    # Live quote — cached 30 seconds
    quote = client.get_quote("AAPL")

    # Company info — cached 30 days
    info = client.get_info("AAPL")

    # Financial statements
    income = client.get_financials("AAPL", statement="income", period="annual")

    # Ratios — use fresh=True to bypass the long-TTL cache
    ratios = client.get_ratios("AAPL", period="annual", fresh=True)

    # Earnings
    earnings = client.get_earnings("AAPL")

    # Insider trades
    trades = client.get_insider_trades("AAPL")
```

### Per-call overrides

```python
bars = client.get_price_history(
    "AAPL",
    date(2024, 1, 1), date(2024, 12, 31),
    no_cache=True,          # bypass cache
    provider="finnhub",     # force a specific provider
    ttl=3600,               # custom TTL in seconds
)
```

## CLI usage

```bash
# Discover what's available
ofclient capabilities         # machine-readable command manifest (JSON)
ofclient version              # package + schema version

# Data commands (all output JSON by default)
ofclient price AAPL --range 1y
ofclient price AAPL --start 2024-01-01 --end 2024-12-31
ofclient quote AAPL
ofclient financials AAPL --statement income --period annual
ofclient info AAPL
ofclient insiders AAPL --since 2024-01-01
ofclient ratios AAPL --period annual --fresh
ofclient earnings AAPL

# Output formats
ofclient price AAPL --range 1m --format table
ofclient price AAPL --range 1m --format csv

# Dry-run: see cache key and whether a fetch would happen, without fetching
ofclient price AAPL --range 1y --dry-run

# Force a provider or bypass cache
ofclient quote AAPL --provider finnhub --no-cache

# Introspection
ofclient cache stats
ofclient providers status
ofclient config show
ofclient config init --output ./config.yaml
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Invalid argument (bad range, unknown statement, etc.) |
| 2 | Provider error or rate limit |
| 3 | Endpoint not supported by any configured provider |
| 4 | Configuration error (missing API key, bad config file) |

### Environment overrides for CLI

| Variable | Effect |
|---|---|
| `OFCLIENT_OUTPUT` | Default output format (`json`, `table`, `csv`) |
| `OFCLIENT_NO_CACHE` | Set `1` to bypass cache on all calls |
| `OFCLIENT_DRY_RUN` | Set `1` to dry-run all calls |
| `OFCLIENT_CONFIG` | Path to config YAML file |

## Provider coverage

| Endpoint | FMP | Finnhub | Twelve Data | YFinance |
|---|---|---|---|---|
| `get_price_history` | ✓ | ✓ | ✓ | ✓ |
| `get_quote` | ✓ | ✓ | ✓ | — |
| `get_info` | ✓ | ✓ | — | ✓ |
| `get_financials` | ✓ | ✓ | — | — |
| `get_ratios` | ✓ | ✓ | — | — |
| `get_earnings` | ✓ | ✓ | — | — |
| `get_insider_trades` | ✓ | ✓ | — | — |

## Running tests

```bash
# Unit tests only (no network)
python -m pytest tests/ -m "not integration"

# All tests including live API calls (requires API keys in env)
python -m pytest tests/ -m integration

# Single provider integration test
python -m pytest tests/integration/test_fmp_live.py -m integration -v
```
