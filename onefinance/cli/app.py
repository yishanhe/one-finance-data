"""OneFinance CLI — agent-first financial data access.

Default output is JSON. Use --format table for human-readable tables.
See design doc §16 for CLI design conventions.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Optional

import typer

from onefinance.cli.format import (
    make_dry_run_envelope,
    make_envelope,
    make_error_envelope,
    print_csv,
    print_json,
    print_table,
)
from onefinance.core.client import OneFinanceClient
from onefinance.core.errors import (
    AllProvidersFailedError,
    ConfigError,
    FinanceError,
    InvalidArgumentError,
    NotSupportedError,
    RateLimitError,
)

app = typer.Typer(
    name="ofclient",
    help="OneFinance CLI — unified financial data access for agents and humans.",
    add_completion=False,
)

config_app = typer.Typer(name="config", help="Configuration commands.")
cache_app = typer.Typer(name="cache", help="Cache inspection commands.")
providers_app = typer.Typer(name="providers", help="Provider status commands.")

app.add_typer(config_app, name="config")
app.add_typer(cache_app, name="cache")
app.add_typer(providers_app, name="providers")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_client(config_path: str | None = None) -> OneFinanceClient:
    return OneFinanceClient(config=config_path)


def _resolve_dates(
    start: str | None,
    end: str | None,
    range_: str | None,
) -> tuple[date, date]:
    today = date.today()
    if range_:
        _range_map = {
            "1m": 30, "3m": 90, "6m": 180,
            "1y": 365, "2y": 730, "5y": 1825,
        }
        days = _range_map.get(range_)
        if days is None:
            raise InvalidArgumentError(
                f"Unknown range '{range_}'. Allowed: 1m, 3m, 6m, 1y, 2y, 5y"
            )
        return today - timedelta(days=days), today
    s = date.fromisoformat(start) if start else today - timedelta(days=365)
    e = date.fromisoformat(end) if end else today
    return s, e


def _emit(envelope: dict, fmt: str) -> None:
    data = envelope.get("data", [])
    if fmt == "table":
        rows = data if isinstance(data, list) else [data]
        print_table(rows, envelope.get("command", ""))
    elif fmt == "csv":
        rows = data if isinstance(data, list) else [data]
        print_csv(rows)
    else:
        print_json(envelope)


def _error_exit(command: str, error: FinanceError) -> None:
    print_json(make_error_envelope(command, error))
    if isinstance(error, InvalidArgumentError):
        raise typer.Exit(1)
    elif isinstance(error, (RateLimitError, AllProvidersFailedError)):
        raise typer.Exit(2)
    elif isinstance(error, NotSupportedError):
        raise typer.Exit(3)
    elif isinstance(error, ConfigError):
        raise typer.Exit(4)
    raise typer.Exit(2)


def _env_bool(var: str) -> bool:
    return os.environ.get(var, "").lower() in ("1", "true", "yes")


def _dry_run_response(command: str, cache_key: str, client: OneFinanceClient) -> None:
    """Print dry-run envelope and return."""
    cached = client.cache.get(cache_key)
    print_json(make_dry_run_envelope(command, {
        "would_fetch": cached is None,
        "cache_hit_predicted": cached is not None,
        "cache_key": cache_key,
    }))


# ---------------------------------------------------------------------------
# price — Type A
# ---------------------------------------------------------------------------

@app.command()
def price(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    start: Optional[str] = typer.Option(None, "--start", help="Start date YYYY-MM-DD"),
    end: Optional[str] = typer.Option(None, "--end", help="End date YYYY-MM-DD"),
    range_: Optional[str] = typer.Option(None, "--range", help="Shorthand range: 1m|3m|6m|1y|2y|5y"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    ttl: Optional[int] = typer.Option(None, "--ttl"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """
    DESCRIPTION
      Fetch daily OHLCV price bars. Type A endpoint — cached with smart TTL
      (30d for historical, 6h after close, 1m during trading hours).

    WHEN TO USE
      Historical price analysis, backtesting, chart data.

    WHEN NOT TO USE
      For the current live price use `ofclient quote`.

    EXAMPLES
      ofclient price AAPL --range 1y
      ofclient price AAPL --start 2024-01-01 --end 2024-12-31
    """
    effective_no_cache = no_cache or _env_bool("OFCLIENT_NO_CACHE")
    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")

    try:
        s, e = _resolve_dates(start, end, range_)
    except (InvalidArgumentError, ValueError) as exc:
        _error_exit("price", InvalidArgumentError(str(exc)))

    if effective_dry_run:
        from onefinance.cache.keys import make_key
        client = _make_client(config)
        key = make_key("price_history", symbol=symbol.upper(), start=s, end=e, interval="1d")
        _dry_run_response("price", key, client)
        return

    try:
        client = _make_client(config)
        bars = client.get_price_history(
            symbol, start=s, end=e,
            no_cache=effective_no_cache, provider=provider, ttl=ttl,
        )
        data = [b.model_dump(mode="json") for b in bars]
        source = bars[0].source if bars else "none"
        envelope = make_envelope("price", data, {
            "source": source,
            "cache_hit": not effective_no_cache,
            "rows": len(data),
        })
        _emit(envelope, fmt)
    except FinanceError as exc:
        _error_exit("price", exc)


# ---------------------------------------------------------------------------
# quote — Type B
# ---------------------------------------------------------------------------

@app.command()
def quote(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """
    DESCRIPTION
      Fetch the current market quote. Type B endpoint — always fetched with
      30-second TTL regardless of cache state.

    WHEN TO USE
      Real-time price checks, alerts, live monitoring.

    WHEN NOT TO USE
      For historical OHLCV bars use `ofclient price`.

    EXAMPLES
      ofclient quote AAPL
      ofclient quote TSLA --format table
    """
    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")
    if effective_dry_run:
        from onefinance.cache.keys import make_key
        client = _make_client(config)
        _dry_run_response("quote", make_key("quote", symbol=symbol.upper()), client)
        return

    try:
        client = _make_client(config)
        q = client.get_quote(
            symbol,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"),
            provider=provider,
        )
        data = q.model_dump(mode="json")
        envelope = make_envelope("quote", data, {
            "source": q.source,
            "cache_hit": not no_cache,
            "rows": 1,
        })
        _emit(envelope, fmt)
    except FinanceError as exc:
        _error_exit("quote", exc)


# ---------------------------------------------------------------------------
# financials — Type A
# ---------------------------------------------------------------------------

_VALID_STATEMENTS = {"income", "balance", "cashflow"}
_VALID_PERIODS = {"annual", "quarterly"}


@app.command()
def financials(
    symbol: str = typer.Argument(...),
    statement: str = typer.Option("income", "--statement", help="income|balance|cashflow"),
    period: str = typer.Option("annual", "--period", help="annual|quarterly"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """
    DESCRIPTION
      Fetch financial statements (income, balance sheet, cash flow). Type A
      endpoint — cached 7 days.

    WHEN TO USE
      Fundamental analysis, DCF modeling, period-over-period comparisons.

    WHEN NOT TO USE
      For pre-computed ratios use `ofclient ratios`.

    EXAMPLES
      ofclient financials AAPL --statement income --period annual
      ofclient financials MSFT --statement balance --period quarterly
    """
    if statement not in _VALID_STATEMENTS:
        _error_exit("financials", InvalidArgumentError(
            f"Invalid statement '{statement}'. Allowed: income, balance, cashflow"
        ))
    if period not in _VALID_PERIODS:
        _error_exit("financials", InvalidArgumentError(
            f"Invalid period '{period}'. Allowed: annual, quarterly"
        ))

    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")
    if effective_dry_run:
        from onefinance.cache.keys import make_key
        client = _make_client(config)
        key = make_key("financials", symbol=symbol.upper(), statement=statement, period=period)
        _dry_run_response("financials", key, client)
        return

    try:
        client = _make_client(config)
        results = client.get_financials(
            symbol, statement=statement, period=period,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"), provider=provider,
        )
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(make_envelope("financials", data, {"source": source, "rows": len(data)}), fmt)
    except FinanceError as exc:
        _error_exit("financials", exc)


# ---------------------------------------------------------------------------
# info — Type A
# ---------------------------------------------------------------------------

@app.command()
def info(
    symbol: str = typer.Argument(...),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """
    DESCRIPTION
      Fetch company profile (name, sector, market cap, etc.). Type A endpoint
      — cached 30 days.

    WHEN TO USE
      Company research, sector classification, metadata lookup.

    WHEN NOT TO USE
      For financial ratios use `ofclient ratios`. For live price use `ofclient quote`.

    EXAMPLES
      ofclient info AAPL
      ofclient info TSLA --format table
    """
    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")
    if effective_dry_run:
        from onefinance.cache.keys import make_key
        client = _make_client(config)
        _dry_run_response("info", make_key("info", symbol=symbol.upper()), client)
        return

    try:
        client = _make_client(config)
        result = client.get_info(
            symbol,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"), provider=provider,
        )
        _emit(make_envelope("info", result.model_dump(mode="json"), {
            "source": result.source, "rows": 1,
        }), fmt)
    except FinanceError as exc:
        _error_exit("info", exc)


# ---------------------------------------------------------------------------
# insiders — Type A
# ---------------------------------------------------------------------------

@app.command()
def insiders(
    symbol: str = typer.Argument(...),
    since: Optional[str] = typer.Option(None, "--since", help="Filter from date YYYY-MM-DD"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """
    DESCRIPTION
      Fetch insider trades (SEC Form 4 filings). Type A endpoint — cached 1 day.

    WHEN TO USE
      Monitoring executive buy/sell activity.

    WHEN NOT TO USE
      Not useful for short-term price analysis.

    EXAMPLES
      ofclient insiders AAPL
      ofclient insiders AAPL --since 2024-01-01
    """
    since_d = date.fromisoformat(since) if since else None

    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")
    if effective_dry_run:
        from onefinance.cache.keys import make_key
        client = _make_client(config)
        key = make_key("insider_trades", symbol=symbol.upper(), since=since_d)
        _dry_run_response("insiders", key, client)
        return

    try:
        client = _make_client(config)
        results = client.get_insider_trades(
            symbol, since=since_d,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"), provider=provider,
        )
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(make_envelope("insiders", data, {"source": source, "rows": len(data)}), fmt)
    except FinanceError as exc:
        _error_exit("insiders", exc)


# ---------------------------------------------------------------------------
# ratios — Type C
# ---------------------------------------------------------------------------

@app.command()
def ratios(
    symbol: str = typer.Argument(...),
    period: str = typer.Option("annual", "--period", help="annual|quarterly"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass long-TTL cache, fetch latest"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """
    DESCRIPTION
      Fetch financial ratios (P/E, P/B, margins, ROE, etc.). Type C endpoint —
      use --fresh to force short-TTL fetch with premium providers first.

    WHEN TO USE
      Valuation screening, comparative analysis.

    WHEN NOT TO USE
      For raw financial statements use `ofclient financials`.

    EXAMPLES
      ofclient ratios AAPL --period annual
      ofclient ratios AAPL --period annual --fresh
    """
    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")
    if effective_dry_run:
        from onefinance.cache.keys import make_key
        client = _make_client(config)
        key = make_key("ratios", symbol=symbol.upper(), period=period, fresh=fresh)
        _dry_run_response("ratios", key, client)
        return

    try:
        client = _make_client(config)
        results = client.get_ratios(
            symbol, period=period, fresh=fresh,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"), provider=provider,
        )
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(make_envelope("ratios", data, {"source": source, "rows": len(data)}), fmt)
    except FinanceError as exc:
        _error_exit("ratios", exc)


# ---------------------------------------------------------------------------
# earnings — Type C
# ---------------------------------------------------------------------------

@app.command()
def earnings(
    symbol: str = typer.Argument(...),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass long-TTL cache, fetch latest"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """
    DESCRIPTION
      Fetch earnings records (EPS actuals vs estimates). Type C endpoint.

    WHEN TO USE
      Earnings surprise analysis, estimate tracking.

    WHEN NOT TO USE
      For full income statement data use `ofclient financials`.

    EXAMPLES
      ofclient earnings AAPL
      ofclient earnings AAPL --fresh
    """
    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")
    if effective_dry_run:
        from onefinance.cache.keys import make_key
        client = _make_client(config)
        key = make_key("earnings", symbol=symbol.upper(), fresh=fresh)
        _dry_run_response("earnings", key, client)
        return

    try:
        client = _make_client(config)
        results = client.get_earnings(
            symbol, fresh=fresh,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"), provider=provider,
        )
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(make_envelope("earnings", data, {"source": source, "rows": len(data)}), fmt)
    except FinanceError as exc:
        _error_exit("earnings", exc)


# ---------------------------------------------------------------------------
# M10 — capabilities and version
# ---------------------------------------------------------------------------

_CAPABILITIES: dict = {
    "schema_version": "1.0",
    "commands": [
        {
            "name": "price",
            "description": "Fetch daily OHLCV price bars. Type A — long TTL, cache-heavy.",
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--start", "required": False, "type": "date", "format": "YYYY-MM-DD"},
                {"name": "--end", "required": False, "type": "date", "format": "YYYY-MM-DD"},
                {"name": "--range", "required": False, "type": "enum", "allowed": ["1m", "3m", "6m", "1y", "2y", "5y"]},
                {"name": "--no-cache", "required": False, "type": "boolean", "default": False},
                {"name": "--provider", "required": False, "type": "string"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient price AAPL --range 1y", "ofclient price AAPL --start 2024-01-01"],
        },
        {
            "name": "quote",
            "description": "Fetch the current market quote. Type B — 30s TTL.",
            "freshness_type": "B",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--no-cache", "required": False, "type": "boolean", "default": False},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient quote AAPL"],
        },
        {
            "name": "financials",
            "description": "Fetch financial statements. Type A — 7-day TTL.",
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--statement", "required": True, "type": "enum", "allowed": ["income", "balance", "cashflow"]},
                {"name": "--period", "required": True, "type": "enum", "allowed": ["annual", "quarterly"]},
                {"name": "--no-cache", "required": False, "type": "boolean", "default": False},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient financials AAPL --statement income --period annual"],
        },
        {
            "name": "info",
            "description": "Fetch company profile. Type A — 30-day TTL.",
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient info AAPL"],
        },
        {
            "name": "insiders",
            "description": "Fetch insider trades (SEC Form 4). Type A — 1-day TTL.",
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--since", "required": False, "type": "date", "format": "YYYY-MM-DD"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient insiders AAPL --since 2024-01-01"],
        },
        {
            "name": "ratios",
            "description": "Fetch financial ratios. Type C — use --fresh for short TTL.",
            "freshness_type": "C",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--period", "required": True, "type": "enum", "allowed": ["annual", "quarterly"]},
                {"name": "--fresh", "required": False, "type": "boolean", "default": False},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient ratios AAPL --period annual", "ofclient ratios AAPL --period annual --fresh"],
        },
        {
            "name": "earnings",
            "description": "Fetch earnings records (EPS vs estimates). Type C.",
            "freshness_type": "C",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--fresh", "required": False, "type": "boolean", "default": False},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient earnings AAPL", "ofclient earnings AAPL --fresh"],
        },
    ],
}


@app.command()
def capabilities():
    """Return machine-readable manifest of all commands and their arguments."""
    print_json(_CAPABILITIES)


@app.command()
def version():
    """Return CLI version and schema version."""
    try:
        from importlib.metadata import version as pkg_version
        v = pkg_version("onefinance")
    except Exception:
        v = "0.1.0"
    print_json({"version": v, "schema_version": "1.0"})


# ---------------------------------------------------------------------------
# M10 — cache, providers, config sub-commands
# ---------------------------------------------------------------------------

@cache_app.command("stats")
def cache_stats(
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
):
    """Show cache statistics: entry count, size, hit/miss counts."""
    try:
        client = _make_client(config)
        stats = client.cache.stats()
        print_json(stats)
    except FinanceError as exc:
        _error_exit("cache stats", exc)


@providers_app.command("status")
def providers_status(
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
):
    """Show per-provider cooldown and health state."""
    try:
        client = _make_client(config)
        state = client.providers.state()
        print_json(state)
    except FinanceError as exc:
        _error_exit("providers status", exc)


@config_app.command("show")
def config_show(
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
):
    """Print current configuration (tiers, cache settings, providers)."""
    try:
        client = _make_client(config)
        cfg = client._config
        print_json({
            "tiers": cfg.tiers,
            "cache": {"dir": cfg.cache.dir, "size_limit_gb": cfg.cache.size_limit_gb},
            "cooldown": {
                "default_initial_s": cfg.cooldown.default_initial_s,
                "max_backoff_s": cfg.cooldown.max_backoff_s,
            },
        })
    except Exception as exc:
        print_json({"error": str(exc)})
        raise typer.Exit(1)


@config_app.command("init")
def config_init(
    output: Optional[str] = typer.Option(None, "--output", help="Path to write config"),
):
    """Generate a config.yaml template with all provider settings."""
    import pathlib

    template = """# OneFinance configuration
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
  price_history: [fmp, finnhub, twelve_data, yfinance]
  financials: [fmp, finnhub, yfinance]
  info: [fmp, finnhub, yfinance]
  insider_trades: [fmp, finnhub]
  quote: [fmp, finnhub, yfinance]
  ratios:
    default: [fmp, finnhub]
    fresh: [fmp, finnhub]
  earnings:
    default: [fmp, finnhub]
    fresh: [fmp, finnhub]

cache:
  dir: ~/.finance_cache
  size_limit_gb: 2

cooldown:
  default_initial_s: 60
  max_backoff_s: 3600
"""
    dest = pathlib.Path(output).expanduser() if output else pathlib.Path("~/.onefinance/config.yaml").expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(template)
    print_json({"status": "created", "path": str(dest)})
