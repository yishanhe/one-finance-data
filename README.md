# one-finance-data

Unified financial data client for Python. Abstracts FMP, Finnhub, Twelve Data, Yahoo Finance, Alpha Vantage, and Massive (formerly Polygon.io) behind a single interface with transparent disk-based caching and a CLI designed for agents and automation.

[![PyPI](https://img.shields.io/pypi/v/onefinance)](https://pypi.org/project/onefinance/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

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
uv sync --all-extras
```

## Setup

### API keys

Each provider requires its own API key. Set the relevant variables in your environment before running the client or CLI. Providers whose key is absent are skipped automatically — `YFinanceProvider` (no key needed) is always available as a fallback.

```bash
export FMP_API_KEY="your_key"           # https://financialmodelingprep.com/developer
export FINNHUB_API_KEY="your_key"       # https://finnhub.io
export TWELVE_DATA_API_KEY="your_key"   # https://twelvedata.com
export ALPHAVANTAGE_API_KEY="your_key"  # https://www.alphavantage.co
export MASSIVE_API_KEY="your_key"       # https://massive.com (formerly Polygon.io)
```

Persist them in your shell profile (`~/.zshrc`, `~/.bashrc`) or use a `.env` file with a tool like [`direnv`](https://direnv.net/).

| Variable | Provider | Free tier |
|---|---|---|
| `FMP_API_KEY` | Financial Modeling Prep | 250 req/day |
| `FINNHUB_API_KEY` | Finnhub | 60 req/min |
| `TWELVE_DATA_API_KEY` | Twelve Data | 800 req/day |
| `ALPHAVANTAGE_API_KEY` | Alpha Vantage | 25 req/day |
| `MASSIVE_API_KEY` | Massive (formerly Polygon.io) | Unlimited calls; 15-min delayed data. Legacy `POLYGON_API_KEY` still honored |
| _(none)_ | SEC EDGAR | No key; free. Optional `EDGAR_USER_AGENT` contact string; 10 req/s |

### CLI environment overrides

| Variable | Effect |
|---|---|
| `OFCLIENT_OUTPUT` | Default output format (`json`, `table`, `csv`) |
| `OFCLIENT_NO_CACHE` | Set `1` to bypass cache on all calls |
| `OFCLIENT_DRY_RUN` | Set `1` to dry-run all calls |
| `OFCLIENT_CONFIG` | Path to a custom config YAML file |

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

    # Batch quotes for multiple symbols — uses native batch endpoints where available
    quotes = client.get_quotes(["AAPL", "MSFT", "GOOG"])

    # Company info — cached 30 days
    info = client.get_info("AAPL")

    # Financial statements
    income = client.get_financials("AAPL", statement="income", period="annual")

    # Ratios — cached once per trading day (date-keyed); use fresh=True for intraday refresh
    ratios = client.get_ratios("AAPL", period="annual", fresh=True)

    # Earnings history
    earnings = client.get_earnings("AAPL")

    # Insider trades
    trades = client.get_insider_trades("AAPL")

    # News
    articles = client.get_news("AAPL")

    # Options chain
    expirations = client.get_options_expirations("AAPL")
    chain = client.get_option_chain("AAPL", expiration=expirations[0])

    # Analyst data
    analyst = client.get_analyst_data("AAPL")

    # Earnings calendar
    calendar = client.get_earnings_calendar(start=date(2026, 7, 1), end=date(2026, 7, 31))

    # DCF valuation
    dcf = client.get_dcf("AAPL")

    # Options analytics (put/call ratio, open interest aggregates)
    analytics = client.get_options_analytics("AAPL")

    # Short interest and days-to-cover
    short = client.get_short_interest("AAPL")

    # Market-wide sentiment (put/call ratio)
    market_sentiment = client.get_market_sentiment()
```

### Per-call overrides

```python
bars = client.get_price_history(
    "AAPL",
    date(2024, 1, 1), date(2024, 12, 31),
    no_cache=True,          # bypass cache for this call
    provider="finnhub",     # force a specific provider
    ttl=3600,               # custom TTL in seconds
)
```

## CLI usage

```bash
# Discovery
ofclient capabilities         # machine-readable command manifest (JSON)
ofclient version              # package + schema version

# Market data
ofclient price AAPL --range 1y
ofclient price AAPL --start 2024-01-01 --end 2024-12-31
ofclient quote AAPL
ofclient quotes AAPL MSFT GOOG              # batch quotes for multiple symbols

# Fundamentals
ofclient financials AAPL --statement income --period annual
ofclient info AAPL
ofclient insiders AAPL --since 2024-01-01
ofclient ratios AAPL --period annual --fresh
ofclient earnings AAPL
ofclient estimates AAPL                     # forward-looking analyst estimates

# Technical analysis
ofclient indicators AAPL                    # MA, MACD, RSI, ATR, bias, trend
ofclient indicators AAPL --range 1y

# Alternative data
ofclient news AAPL
ofclient actions AAPL                       # dividends and splits
ofclient holders AAPL                       # institutional holders
ofclient analyst AAPL                       # price targets and ratings
ofclient options-analytics AAPL            # put/call ratio, open interest aggregates
ofclient short-interest AAPL               # short interest and days-to-cover
ofclient sentiment                          # market-wide put/call ratio

# Options
ofclient options AAPL                       # list available expiration dates
ofclient options AAPL --expiration 2026-06-20   # full options chain

# Market-wide
ofclient screen "sector=Technology"
ofclient sector technology
ofclient calendar                           # upcoming earnings releases
ofclient calendar --start 2026-07-01 --end 2026-07-31

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
ofclient providers check                    # validate API keys, instantiation, tier refs
ofclient providers check --ping             # also ping each provider and report latency
ofclient config show
ofclient config init --output ./config.yaml

# Audit log
ofclient audit stats
ofclient audit recent --limit 20 --format table
ofclient audit path
ofclient audit follow                       # tail live entries (Ctrl-C to stop)
ofclient audit follow --status error        # stream errors only
ofclient audit truncate --confirm           # permanently clear all entries
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Invalid argument (bad range, unknown statement, etc.) |
| 2 | Provider error or rate limit |
| 3 | Endpoint not supported by any configured provider |
| 4 | Configuration error (missing API key, bad config file) |

## Provider coverage

| Endpoint | FMP | Finnhub | Twelve Data | YFinance | Alpha Vantage | Massive | EDGAR |
|---|---|---|---|---|---|---|---|
| `get_price_history` | ✓ | ✓* | ✓ | ✓ | ✓ | ✓ | — |
| `get_quote` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| `get_quotes` (native batch) | — | — | ✓ | — | — | — | — |
| `get_info` | ✓ | ✓ | — | ✓ | ✓ | ✓ | — |
| `get_financials` | ✓ | ✓ | — | ✓ | ✓ | — | ✓ |
| `get_ratios` | ✓ | ✓ | — | ✓ | ✓ | — | — |
| `get_earnings` | ✓ | ✓ | — | ✓ | ✓ | — | — |
| `get_insider_trades` | ✓ | ✓ | — | ✓ | — | — | — |
| `get_dcf` | ✓ | — | — | — | — | — | — |
| `get_news` | — | ✓ | — | ✓ | ✓ | ✓ | — |
| `get_corporate_actions` | ✓ | ✓ | — | ✓ | — | ✓ | — |
| `get_institutional_holders` | ✓ | — | — | ✓ | — | — | — |
| `get_analyst_data` | ✓ | ✓ | — | ✓ | — | — | — |
| `get_options_expirations` | — | — | — | ✓ | — | — | — |
| `get_option_chain` | — | — | — | ✓ | — | — | — |
| `get_sector_overview` | — | — | — | ✓ | — | — | — |
| `get_earnings_calendar` | ✓ | ✓ | — | — | — | — | — |
| `get_forward_estimates` | ✓ | ✓ | — | ✓ | — | — | — |
| `get_options_analytics` | — | — | — | ✓ (derived) | — | — | — |
| `get_short_interest` | ✓ | — | — | ✓ | — | — | — |
| `get_market_sentiment` | ✓ | — | — | — | — | — | — |

> **Note on batch quotes:** `get_quotes` uses Twelve Data's native multi-symbol endpoint when available. For other providers, it fans out concurrent single `get_quote` calls automatically.

> \* Finnhub free-tier returns HTTP 403 for price history; treated as plan restriction (NotSupportedError). Paid plans may work.

## Running tests

```bash
# Unit tests only (no network)
uv run pytest tests/ -m "not integration"

# All tests including live API calls (requires API keys in env)
uv run pytest tests/ -m integration

# Single test file
uv run pytest tests/unit/test_client.py -v
```
