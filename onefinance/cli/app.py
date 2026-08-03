"""OneFinance CLI — agent-first financial data access.

Default output is JSON. Use --format table for human-readable tables.
See design doc §16 for CLI design conventions.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from typing import Any

import typer

from onefinance.cli._admin_commands import register_admin_commands
from onefinance.cli._audit_commands import register_audit_commands
from onefinance.cli._capabilities import build_capabilities
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

BANNER = r"""[bold cyan]
                 ╔═╗╔╗╔╔═╗                    
                 ║ ║║║║║╣                      
                 ╚═╝╝╚╝╚═╝                    
     ╔═╗╦╔╗╔╔═╗╔╗╔╔═╗╔═╗  ╔╦╗╔═╗╔╦╗╔═╗
     ╠╣ ║║║║╠═╣║║║║  ║╣    ║║╠═╣ ║ ╠═╣
     ╚  ╩╝╚╝╩ ╩╝╚╝╚═╝╚═╝  ═╩╝╩ ╩ ╩ ╩ ╩[/bold cyan]
[dim]       unified financial data client[/dim]
"""


def _banner_callback(ctx: typer.Context) -> None:
    """Print ASCII banner when no subcommand is given or --help is used."""
    if ctx.invoked_subcommand is None:
        from rich.console import Console

        Console(stderr=True).print(BANNER, highlight=False)


_HELP_OPTS = {"help_option_names": ["-h", "--help"]}

# Shared option help strings — every command help must be self-explanatory for
# agents that only ever see `--help` output.
_HELP_NO_CACHE = "Bypass cache read and fetch live. The result is still written to cache."
_HELP_PROVIDER = (
    "Force one provider by name (e.g. yfinance, fmp, finnhub) instead of the tier list."
)
_HELP_TTL = "Override the cache TTL (seconds) for this call's result."
_HELP_FORMAT = "Output format: json, table, or csv. Default json (env: OFCLIENT_OUTPUT)."
_HELP_CONFIG = "Path to a YAML config file (env: OFCLIENT_CONFIG)."
_HELP_DRY_RUN = "Do not call providers; report the cache key and whether a fetch would happen."

app = typer.Typer(
    name="ofclient",
    help="""OneFinance CLI — unified financial data access for agents and humans.

    OUTPUT CONTRACT (agents, read this)
      Every command prints one JSON envelope to stdout:
      {"schema_version": "1.0", "status": "success|error", "command": ...,
       "data": ..., "metadata": {"source": provider, "rows": ...}}.
      On error, data is replaced by "error": {"code", "message", "retry_safe",
      "retry_after_seconds"} — branch on the stable "code" string.

    EXIT CODES
      0 success · 1 invalid arguments · 2 provider failure / rate limit ·
      3 endpoint not supported · 4 configuration error

    DISCOVERY
      `ofclient capabilities` returns a machine-readable manifest of every
      command, argument, type, and allowed values. `ofclient providers check`
      validates API keys; `ofclient doctor` diagnoses setup issues.

    ENVIRONMENT
      OFCLIENT_OUTPUT (json|table|csv) · OFCLIENT_CONFIG (config path) ·
      OFCLIENT_NO_CACHE=1 · OFCLIENT_DRY_RUN=1 · provider API keys
      (FMP_API_KEY, FINNHUB_API_KEY, ... — see `ofclient providers check`).
    """,
    add_completion=False,
    invoke_without_command=True,
    callback=_banner_callback,
    context_settings=_HELP_OPTS,
)

config_app = typer.Typer(name="config", help="Configuration commands.", context_settings=_HELP_OPTS)
cache_app = typer.Typer(
    name="cache", help="Cache inspection commands.", context_settings=_HELP_OPTS
)
providers_app = typer.Typer(
    name="providers", help="Provider status commands.", context_settings=_HELP_OPTS
)
audit_app = typer.Typer(name="audit", help="Audit log commands.", context_settings=_HELP_OPTS)

app.add_typer(config_app, name="config")
app.add_typer(cache_app, name="cache")
app.add_typer(providers_app, name="providers")
app.add_typer(audit_app, name="audit")


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
            "1m": 30,
            "3m": 90,
            "6m": 180,
            "1y": 365,
            "2y": 730,
            "5y": 1825,
        }
        days = _range_map.get(range_)
        if days is None:
            raise InvalidArgumentError(f"Unknown range '{range_}'. Allowed: 1m, 3m, 6m, 1y, 2y, 5y")
        return today - timedelta(days=days), today
    s = date.fromisoformat(start) if start else today - timedelta(days=365)
    e = date.fromisoformat(end) if end else today
    return s, e


def _emit(envelope: dict[str, Any], fmt: str) -> None:
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


def _price_age_seconds(quote_timestamp: datetime) -> float:
    """Seconds between now and a quote's own timestamp — surfaces provider staleness.

    Some providers (yfinance during RTH, ADR quotes) serve a previous-close
    price with its own stale timestamp rather than erroring. Exposing this
    lets callers detect and react to staleness instead of trusting `price`
    at face value.
    """
    return round((datetime.now(UTC) - quote_timestamp).total_seconds(), 1)


def _dry_run_response(command: str, cache_key: str, client: OneFinanceClient) -> None:
    """Print dry-run envelope and return."""
    cached = client.cache.get(cache_key)
    print_json(
        make_dry_run_envelope(
            command,
            {
                "would_fetch": cached is None,
                "cache_hit_predicted": cached is not None,
                "cache_key": cache_key,
            },
        )
    )


# ---------------------------------------------------------------------------
# price — Type A
# ---------------------------------------------------------------------------


@app.command()
def price(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    start: str | None = typer.Option(None, "--start", help="Start date YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--end", help="End date YYYY-MM-DD"),
    range_: str | None = typer.Option(None, "--range", help="Shorthand range: 1m|3m|6m|1y|2y|5y"),
    adjusted: bool = typer.Option(False, "--adjusted", help="Replace close with adj_close"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    ttl: int | None = typer.Option(None, "--ttl", help=_HELP_TTL),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
    Fetch daily OHLCV price bars. Type A endpoint — cached with smart TTL
    (30d for historical, 6h after close, 1m during trading hours).

    WHEN TO USE
      Historical price analysis, backtesting, chart data.

    WHEN NOT TO USE
      For the current live price use `ofclient quote`.

    EXAMPLES
      ofclient price AAPL --range 1y
      ofclient price AAPL --start 2024-01-01 --end 2024-12-31
      ofclient price AAPL --range 1y --adjusted   # close = dividend-adjusted close
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
            symbol,
            start=s,
            end=e,
            no_cache=effective_no_cache,
            provider=provider,
            ttl=ttl,
        )
        data = [b.model_dump(mode="json") for b in bars]
        if adjusted:
            for row in data:
                row["close"] = row["adj_close"]
        source = bars[0].source if bars else "none"
        envelope = make_envelope(
            "price",
            data,
            {
                "source": source,
                "cache_hit": not effective_no_cache,
                "rows": len(data),
                "adjusted": adjusted,
            },
        )
        _emit(envelope, fmt)
    except FinanceError as exc:
        _error_exit("price", exc)


# ---------------------------------------------------------------------------
# quote — Type B
# ---------------------------------------------------------------------------


@app.command()
def quote(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
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
    if "," in symbol or any(c.isspace() for c in symbol):
        _error_exit(
            "quote",
            InvalidArgumentError(
                f"'{symbol}' looks like multiple symbols. "
                "Use `ofclient quotes SYM1 SYM2 ...` for batch quotes."
            ),
        )
        return

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
        data["price_age_seconds"] = _price_age_seconds(q.timestamp)
        envelope = make_envelope(
            "quote",
            data,
            {
                "source": q.source,
                "cache_hit": not no_cache,
                "rows": 1,
            },
        )
        _emit(envelope, fmt)
    except FinanceError as exc:
        _error_exit("quote", exc)


@app.command()
def quotes(
    symbols: list[str] = typer.Argument(..., help="List of ticker symbols, e.g. AAPL MSFT"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
    Fetch current market quotes for multiple symbols as a batch.

    EXAMPLES
      ofclient quotes AAPL MSFT GOOG
      ofclient quotes AAPL MSFT TSLA --format table
    """
    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")
    if effective_dry_run:
        from onefinance.cache.keys import make_key

        client = _make_client(config)
        for sym in symbols:
            _dry_run_response("quotes", make_key("quote", symbol=sym.upper()), client)
        return

    try:
        client = _make_client(config)
        results = client.get_quotes(
            symbols,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"),
            provider=provider,
        )

        valid_data = []
        errors = {}
        for sym, res in zip(symbols, results):
            if isinstance(res, FinanceError):
                errors[sym] = res.message
            else:
                item = res.model_dump(mode="json")
                item["price_age_seconds"] = _price_age_seconds(res.timestamp)
                valid_data.append(item)

        if not valid_data and errors:
            # If everything failed, raise the first error to exit properly
            _error_exit("quotes", next(r for r in results if isinstance(r, FinanceError)))

        envelope = make_envelope(
            "quotes",
            valid_data,
            {
                "rows": len(valid_data),
                "errors": errors if errors else None,
            },
        )
        _emit(envelope, fmt)
    except FinanceError as exc:
        _error_exit("quotes", exc)


# ---------------------------------------------------------------------------
# financials — Type A
# ---------------------------------------------------------------------------

_VALID_STATEMENTS = {"income", "balance", "cashflow"}
_VALID_PERIODS = {"annual", "quarterly"}


@app.command()
def financials(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    statement: str = typer.Option("income", "--statement", help="income|balance|cashflow"),
    period: str = typer.Option("annual", "--period", help="annual|quarterly"),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Max number of periods to return (most recent first)"
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
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
        _error_exit(
            "financials",
            InvalidArgumentError(
                f"Invalid statement '{statement}'. Allowed: income, balance, cashflow"
            ),
        )
    if period not in _VALID_PERIODS:
        _error_exit(
            "financials",
            InvalidArgumentError(f"Invalid period '{period}'. Allowed: annual, quarterly"),
        )

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
            symbol,
            statement=statement,
            period=period,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"),
            provider=provider,
        )
        if limit is not None and limit > 0:
            results = results[:limit]
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
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
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
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"),
            provider=provider,
        )
        _emit(
            make_envelope(
                "info",
                result.model_dump(mode="json"),
                {
                    "source": result.source,
                    "rows": 1,
                },
            ),
            fmt,
        )
    except FinanceError as exc:
        _error_exit("info", exc)


@app.command()
def infos(
    symbols: list[str] = typer.Argument(..., help="List of ticker symbols, e.g. AAPL MSFT"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
    Fetch company profiles for multiple symbols as a batch.

    EXAMPLES
      ofclient infos AAPL MSFT GOOG
      ofclient infos AAPL MSFT TSLA --format table
    """
    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")
    if effective_dry_run:
        from onefinance.cache.keys import make_key

        client = _make_client(config)
        for sym in symbols:
            _dry_run_response("infos", make_key("info", symbol=sym.upper()), client)
        return

    try:
        client = _make_client(config)
        results = client.get_infos(
            symbols,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"),
            provider=provider,
        )

        valid_data = []
        errors = {}
        for sym, res in zip(symbols, results, strict=False):
            if isinstance(res, FinanceError):
                errors[sym] = res.message
            else:
                valid_data.append(res.model_dump(mode="json"))

        if not valid_data and errors:
            _error_exit("infos", next(r for r in results if isinstance(r, FinanceError)))

        envelope = make_envelope(
            "infos",
            valid_data,
            {
                "rows": len(valid_data),
                "errors": errors if errors else None,
            },
        )
        _emit(envelope, fmt)
    except FinanceError as exc:
        _error_exit("infos", exc)


# ---------------------------------------------------------------------------
# insiders — Type A
# ---------------------------------------------------------------------------


@app.command()
def insiders(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    since: str | None = typer.Option(None, "--since", help="Filter from date YYYY-MM-DD"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
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
            symbol,
            since=since_d,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"),
            provider=provider,
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
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    period: str = typer.Option("annual", "--period", help="annual|quarterly"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass long-TTL cache, fetch latest"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
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
            symbol,
            period=period,
            fresh=fresh,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"),
            provider=provider,
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
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass long-TTL cache, fetch latest"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
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
            symbol,
            fresh=fresh,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"),
            provider=provider,
        )
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(make_envelope("earnings", data, {"source": source, "rows": len(data)}), fmt)
    except FinanceError as exc:
        _error_exit("earnings", exc)


# ---------------------------------------------------------------------------
# indicators — derived from price_history
# ---------------------------------------------------------------------------


@app.command()
def indicators(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    start: str | None = typer.Option(None, "--start", help="Start date YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--end", help="End date YYYY-MM-DD"),
    range_: str | None = typer.Option(
        None,
        "--range",
        help="Shorthand range: 1m|3m|6m|1y|2y|5y (default 6m)",
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    ttl: int | None = typer.Option(None, "--ttl", help=_HELP_TTL),
    no_quote: bool = typer.Option(
        False,
        "--no-quote",
        help="Skip the live-quote fetch (no *_current fields, no staleness flag)",
    ),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
    Compute a snapshot of technical indicators from daily OHLCV bars.
    Derived from the same data as `price`; shares the price_history cache.

      All values are computed from the last COMPLETED daily bar (`as_of`,
      `last_close`) — intraday action is never included. A live quote is
      also fetched (30s-cached; disable with --no-quote) to populate the
      current-price-classified level fields and the staleness flag:
        reference_price            the live quote used for classification
        support_levels_current     candidates strictly BELOW the live quote
        resistance_levels_current  candidates strictly ABOVE the live quote
        indicator_stale            true when bars are missing >=1 completed
                                   trading session relative to the quote
        stale_reason               human-readable explanation when stale

    INDICATORS RETURNED
      Moving averages (close-based, simple):
        ma5, ma10, ma20, ma60      None if not enough bars

      Bias (price deviation from MA, in %):
        bias_ma5, bias_ma10, bias_ma20
        bias_status                safe (<2%) | caution (<5%) | danger | unknown

      Trend:
        ma_alignment               bullish | bearish | mixed | unknown
        trend_status               STRONG_BULL | BULL | NEUTRAL | BEAR | STRONG_BEAR

      Momentum:
        macd_dif, macd_dea, macd_bar   MACD(12, 26, 9)
        rsi14                          RSI(14), Wilder smoothing, range 0-100
        rsi14_prev                     RSI(14) of the previous completed bar
        rsi14_change                   rsi14 - rsi14_prev, in RSI points
        rsi_direction                  rising (>+1pt) | falling (<-1pt) |
                                       flat | unknown (no prior RSI)

      Volatility:
        atr14                          ATR(14), Wilder smoothing
        atr_pct                        atr14 / close * 100

      Volume:
        volume_ratio                   last volume / 5-day MA (excl. last bar)

      Levels (classified vs the LAST BAR CLOSE — see *_current above for
      live-quote classification):
        support_levels                 MAs below last bar close (high to low)
        resistance_levels              Recent 20-bar highs above last bar close
                                       (low to high, top 3)

    DATA REQUIREMENTS
      Needs >=5 bars; MA20 needs >=20; MA60 needs >=60; MACD needs >=26;
      RSI14 and ATR14 need >=15; rsi_direction needs >=16 (one extra bar
      for the prior RSI). Default --range 6m covers all.

    WHEN TO USE
      Quick technical snapshot for a symbol.

    WHEN NOT TO USE
      For raw OHLCV bars use `ofclient price`.
      For fundamental ratios use `ofclient ratios`.

    EXAMPLES
      ofclient indicators AAPL
      ofclient indicators AAPL --range 1y
      ofclient indicators AAPL --start 2024-01-01 --end 2024-12-31
      ofclient indicators AAPL --format table
    """
    effective_no_cache = no_cache or _env_bool("OFCLIENT_NO_CACHE")
    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")

    if not (start or end or range_):
        range_ = "6m"

    try:
        s, e = _resolve_dates(start, end, range_)
    except (InvalidArgumentError, ValueError) as exc:
        _error_exit("indicators", InvalidArgumentError(str(exc)))

    if effective_dry_run:
        from onefinance.cache.keys import make_key

        client = _make_client(config)
        key = make_key("price_history", symbol=symbol.upper(), start=s, end=e, interval="1d")
        _dry_run_response("indicators", key, client)
        return

    try:
        client = _make_client(config)
        ind = client.get_indicators(
            symbol,
            start=s,
            end=e,
            no_cache=effective_no_cache,
            provider=provider,
            ttl=ttl,
            with_quote=not no_quote,
        )
        bars = client.get_price_history(
            symbol,
            start=s,
            end=e,
            no_cache=False,
            provider=provider,
        )
        data = {
            "symbol": symbol.upper(),
            **ind.model_dump(mode="json"),
        }
        envelope = make_envelope(
            "indicators",
            data,
            {
                "source": bars[0].source if bars else "none",
                "cache_hit": not effective_no_cache,
                "bars": len(bars),
            },
        )
        _emit(envelope, fmt)
    except ValueError as exc:
        _error_exit("indicators", InvalidArgumentError(str(exc)))
    except FinanceError as exc:
        _error_exit("indicators", exc)


# ---------------------------------------------------------------------------
# Alternative Data
# ---------------------------------------------------------------------------


@app.command()
def news(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of entries to return."),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """Fetch recent news articles."""
    if dry_run or _env_bool("OFCLIENT_DRY_RUN"):
        from onefinance.cache.keys import make_key

        client = _make_client(config)
        key = make_key("news", symbol=symbol.upper(), limit=limit)
        _dry_run_response("news", key, client)
        return

    try:
        client = _make_client(config)
        results = client.get_news(symbol, limit=limit, no_cache=no_cache, provider=provider)
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(make_envelope("news", data, {"source": source, "rows": len(data)}), fmt)
    except FinanceError as exc:
        _error_exit("news", exc)


@app.command()
def actions(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """Fetch corporate actions (dividends and splits)."""
    if dry_run or _env_bool("OFCLIENT_DRY_RUN"):
        from onefinance.cache.keys import make_key

        client = _make_client(config)
        key = make_key("corporate_actions", symbol=symbol.upper())
        _dry_run_response("actions", key, client)
        return

    try:
        client = _make_client(config)
        results = client.get_corporate_actions(symbol, no_cache=no_cache, provider=provider)
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(make_envelope("actions", data, {"source": source, "rows": len(data)}), fmt)
    except FinanceError as exc:
        _error_exit("actions", exc)


@app.command()
def holders(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """Fetch top institutional holders."""
    if dry_run or _env_bool("OFCLIENT_DRY_RUN"):
        from onefinance.cache.keys import make_key

        client = _make_client(config)
        key = make_key("institutional_holders", symbol=symbol.upper())
        _dry_run_response("holders", key, client)
        return

    try:
        client = _make_client(config)
        results = client.get_institutional_holders(symbol, no_cache=no_cache, provider=provider)
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(make_envelope("holders", data, {"source": source, "rows": len(data)}), fmt)
    except FinanceError as exc:
        _error_exit("holders", exc)


@app.command()
def options(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    expiration: str | None = typer.Option(
        None,
        "--expiration",
        "-e",
        help="YYYY-MM-DD. If omitted, returns available expiration dates.",
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
    Two distinct outputs from one command, gated by --expiration:
      no --expiration  -> a plain list of available expiration DATE STRINGS
                           (e.g. ["2026-06-19", ...]), NOT an option chain.
      --expiration DATE -> the actual option chain (calls/puts, strikes,
                           greeks if the provider supports them) for that date.

      First-time callers often expect a chain from the bare form and get
      confused by a list of dates instead — this is that behavior, working
      as designed. Always pass --expiration once you know which date you want.

    EXAMPLES
      ofclient options AAPL                        # list expiration dates
      ofclient options AAPL --expiration 2026-06-19 # the actual chain
      ofclient options AAPL -e 2026-06-19 --format table
    """
    if dry_run or _env_bool("OFCLIENT_DRY_RUN"):
        from onefinance.cache.keys import make_key

        client = _make_client(config)
        if not expiration:
            key = make_key("options_expirations", symbol=symbol.upper())
            _dry_run_response("options", key, client)
        else:
            try:
                exp_d = date.fromisoformat(expiration)
            except ValueError:
                _error_exit(
                    "options",
                    InvalidArgumentError(f"Invalid expiration date: {expiration}. Use YYYY-MM-DD."),
                )
            key = make_key("option_chain", symbol=symbol.upper(), expiration=exp_d)
            _dry_run_response("options", key, client)
        return

    try:
        client = _make_client(config)
        if not expiration:
            dates = client.get_options_expirations(symbol, no_cache=no_cache, provider=provider)
            data = [d.isoformat() for d in dates]
            _emit(
                make_envelope(
                    "options_expirations", data, {"symbol": symbol.upper(), "count": len(dates)}
                ),
                fmt,
            )
        else:
            try:
                exp_d = date.fromisoformat(expiration)
            except ValueError:
                _error_exit(
                    "options",
                    InvalidArgumentError(f"Invalid expiration date: {expiration}. Use YYYY-MM-DD."),
                )
                return
            chain = client.get_option_chain(symbol, exp_d, no_cache=no_cache, provider=provider)
            chain_data = chain.model_dump(mode="json")
            _emit(
                make_envelope(
                    "option_chain",
                    chain_data,
                    {"source": chain.source, "calls": len(chain.calls), "puts": len(chain.puts)},
                ),
                fmt,
            )
    except FinanceError as exc:
        _error_exit("options", exc)


@app.command()
def options_analytics(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    max_expirations: int = typer.Option(
        6, "--max-expirations", "-n", help="Max expiration dates to aggregate."
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
) -> None:
    """Fetch aggregated put/call ratio and open interest for SYMBOL."""
    try:
        client = _make_client(config)
        result = client.get_options_analytics(
            symbol, max_expirations=max_expirations, no_cache=no_cache, provider=provider
        )
        data = result.model_dump(mode="json")
        _emit(
            make_envelope(
                "options_analytics",
                data,
                {
                    "symbol": symbol.upper(),
                    "expirations_used": result.expirations_used,
                    "source": result.source,
                },
            ),
            fmt,
        )
    except FinanceError as exc:
        _error_exit("options-analytics", exc)


@app.command()
def gex(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    max_expirations: int = typer.Option(
        6, "--max-expirations", "-n", help="Max expiration dates to aggregate."
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
) -> None:
    """Compute dealer gamma-exposure (GEX) profile for SYMBOL.

    Requires option chains containing per-contract gamma.
    """
    try:
        client = _make_client(config)
        result = client.get_gex(
            symbol, max_expirations=max_expirations, no_cache=no_cache, provider=provider
        )
        data = result.model_dump(mode="json")
        _emit(
            make_envelope(
                "gex",
                data,
                {
                    "symbol": symbol.upper(),
                    "expirations_used": result.expirations_used,
                    "source": result.source,
                },
            ),
            fmt,
        )
    except ValueError as exc:
        _error_exit("gex", InvalidArgumentError(str(exc)))
    except FinanceError as exc:
        _error_exit("gex", exc)


@app.command()
def maxpain(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    expiration: str = typer.Option(..., "--expiration", "-e", help="YYYY-MM-DD."),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
) -> None:
    """Compute the max-pain strike for SYMBOL at EXPIRATION."""
    try:
        exp_d = date.fromisoformat(expiration)
    except ValueError:
        _error_exit(
            "maxpain",
            InvalidArgumentError(f"Invalid expiration date: {expiration}. Use YYYY-MM-DD."),
        )
        return

    try:
        client = _make_client(config)
        result = client.get_max_pain(symbol, exp_d, no_cache=no_cache, provider=provider)
        data = result.model_dump(mode="json")
        _emit(
            make_envelope(
                "maxpain",
                data,
                {
                    "symbol": symbol.upper(),
                    "max_pain_strike": result.max_pain_strike,
                    "source": result.source,
                },
            ),
            fmt,
        )
    except ValueError as exc:
        _error_exit("maxpain", InvalidArgumentError(str(exc)))
    except FinanceError as exc:
        _error_exit("maxpain", exc)


@app.command()
def short_interest(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
) -> None:
    """Fetch short interest and days-to-cover for SYMBOL."""
    try:
        client = _make_client(config)
        result = client.get_short_interest(symbol, no_cache=no_cache, provider=provider)
        data = result.model_dump(mode="json")
        _emit(
            make_envelope(
                "short_interest",
                data,
                {"symbol": symbol.upper(), "source": result.source},
            ),
            fmt,
        )
    except FinanceError as exc:
        _error_exit("short-interest", exc)


@app.command()
def sentiment(
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
) -> None:
    """Fetch market-wide put/call ratio data."""
    try:
        client = _make_client(config)
        result = client.get_market_sentiment(no_cache=no_cache, provider=provider)
        data = result.model_dump(mode="json")
        _emit(
            make_envelope(
                "market_sentiment",
                data,
                {"source": result.source},
            ),
            fmt,
        )
    except FinanceError as exc:
        _error_exit("sentiment", exc)


@app.command()
def screen(
    query: str = typer.Argument(
        ..., help="Query string for screener (e.g. 'marketCapMoreThan=1000000000')"
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
    Screen individual stocks with an FMP-style query string
    (marketCapMoreThan, sector, industry, etc. as URL-encoded params).

    WHEN NOT TO USE
      For a sector-level overview (aggregate stats for a whole sector),
      use `ofclient sector <name>` instead — this command returns a list
      of matching tickers, not a sector summary, and a sector-only query
      string here will typically 404 against the underlying screener API.

    EXAMPLES
      ofclient screen "marketCapMoreThan=1000000000&sector=Technology"
      ofclient sector technology   # sector-level overview, not this command
    """
    if dry_run or _env_bool("OFCLIENT_DRY_RUN"):
        from onefinance.cache.keys import make_key

        client = _make_client(config)
        key = make_key("screen_stocks", query=query)
        _dry_run_response("screen", key, client)
        return

    try:
        client = _make_client(config)
        results = client.screen_stocks(query, no_cache=no_cache, provider=provider)
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(make_envelope("screen_stocks", data, {"source": source, "rows": len(data)}), fmt)
    except FinanceError as exc:
        _error_exit("screen", exc)


@app.command()
def sector(
    name: str = typer.Argument(..., help="Sector name (e.g. 'technology')"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """Fetch sector overview."""
    if dry_run or _env_bool("OFCLIENT_DRY_RUN"):
        from onefinance.cache.keys import make_key

        client = _make_client(config)
        key = make_key("sector_overview", sector=name.lower())
        _dry_run_response("sector", key, client)
        return

    try:
        client = _make_client(config)
        result = client.get_sector_overview(name, no_cache=no_cache, provider=provider)
        data = result.model_dump(mode="json")
        _emit(
            make_envelope(
                "sector_overview",
                data,
                {"source": result.source, "companies": len(result.top_companies or [])},
            ),
            fmt,
        )
    except FinanceError as exc:
        _error_exit("sector", exc)


@app.command()
def analyst(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """Fetch analyst price targets and ratings."""
    if dry_run or _env_bool("OFCLIENT_DRY_RUN"):
        from onefinance.cache.keys import make_key

        client = _make_client(config)
        key = make_key("analyst_data", symbol=symbol.upper())
        _dry_run_response("analyst", key, client)
        return

    try:
        client = _make_client(config)
        result = client.get_analyst_data(symbol, no_cache=no_cache, provider=provider)
        data = result.model_dump(mode="json")
        _emit(make_envelope("analyst", data, {"source": result.source}), fmt)
    except FinanceError as exc:
        _error_exit("analyst", exc)


@app.command()
def peers(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
) -> None:
    """
    Fetch peer/comparable companies for a ticker. Type A endpoint — cached 7 days.

    WHEN TO USE
      Finding comparable companies for ratio analysis, peer benchmarking.

    WHEN NOT TO USE
      For fundamental ratios across peers, fetch each with `ofclient ratios`.

    EXAMPLES
      ofclient peers AAPL
      ofclient peers MSFT --format table
    """
    effective_no_cache = no_cache or _env_bool("OFCLIENT_NO_CACHE")

    try:
        client = _make_client(config)
        result = client.get_peers(symbol, no_cache=effective_no_cache, provider=provider)
        data = [p.model_dump(mode="json") for p in result]
        source = result[0].source if result else "none"
        envelope = make_envelope(
            "peers",
            data,
            {
                "source": source,
                "symbol": symbol.upper(),
                "count": len(data),
            },
        )
        _emit(envelope, fmt)
    except FinanceError as exc:
        _error_exit("peers", exc)


@app.command()
def calendar(
    start: str | None = typer.Option(None, "--start", help="Start date YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--end", help="End date YYYY-MM-DD"),
    symbol: str | None = typer.Option(None, "--symbol", "-s", help="Filter by ticker"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
    Fetch the earnings calendar for a date range. Returns scheduled
    earnings releases with EPS and revenue estimates. Type A endpoint
    — cached 4 hours.

    WHEN TO USE
      Discovering upcoming earnings events, earnings-season planning.

    WHEN NOT TO USE
      For historical per-symbol EPS actuals use `ofclient earnings`.

    EXAMPLES
      ofclient calendar
      ofclient calendar --start 2025-07-01 --end 2025-07-31
      ofclient calendar --symbol AAPL
    """
    from onefinance.cache.keys import make_key

    effective_no_cache = no_cache or _env_bool("OFCLIENT_NO_CACHE")
    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")

    start_d = date.fromisoformat(start) if start else date.today()
    end_d = date.fromisoformat(end) if end else date.today() + timedelta(days=7)

    if effective_dry_run:
        client = _make_client(config)
        key = make_key("earnings_calendar", start=start_d, end=end_d)
        _dry_run_response("calendar", key, client)
        return

    try:
        client = _make_client(config)
        results = client.get_earnings_calendar(
            start=start_d,
            end=end_d,
            symbol=symbol,
            no_cache=effective_no_cache,
            provider=provider,
        )
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(
            make_envelope(
                "calendar",
                data,
                {
                    "source": source,
                    "rows": len(data),
                    "start": start_d.isoformat(),
                    "end": end_d.isoformat(),
                },
            ),
            fmt,
        )
    except FinanceError as exc:
        _error_exit("calendar", exc)


@app.command()
def macro(
    start: str | None = typer.Option(None, "--start", help="Start date YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--end", help="End date YYYY-MM-DD"),
    country: str | None = typer.Option(None, "--country", "-c", help="ISO country code, e.g. US"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
    Fetch the macro economic calendar for a date range. Returns scheduled
    releases: CPI, GDP, FOMC, NFP, PMI, retail sales, and more. Type A
    endpoint — cached 4 hours.

    WHEN TO USE
      Anticipating market-moving events, macro-overlay analysis, event-driven
      strategies.

    WHEN NOT TO USE
      For earnings events use `ofclient calendar`. For per-symbol historical
      EPS use `ofclient earnings`.

    EXAMPLES
      ofclient macro
      ofclient macro --start 2025-07-01 --end 2025-07-31
      ofclient macro --country US
    """
    from onefinance.cache.keys import make_key

    effective_no_cache = no_cache or _env_bool("OFCLIENT_NO_CACHE")
    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")

    start_d = date.fromisoformat(start) if start else date.today()
    end_d = date.fromisoformat(end) if end else date.today() + timedelta(days=7)

    if effective_dry_run:
        client = _make_client(config)
        key = make_key("economic_calendar", start=start_d, end=end_d)
        _dry_run_response("macro", key, client)
        return

    try:
        client = _make_client(config)
        results = client.get_economic_calendar(
            start=start_d,
            end=end_d,
            country=country,
            no_cache=effective_no_cache,
            provider=provider,
        )
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(
            make_envelope(
                "macro",
                data,
                {
                    "source": source,
                    "rows": len(data),
                    "start": start_d.isoformat(),
                    "end": end_d.isoformat(),
                },
            ),
            fmt,
        )
    except FinanceError as exc:
        _error_exit("macro", exc)


@app.command()
def treasury(
    start: str | None = typer.Option(None, "--start", help="Start date YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--end", help="End date YYYY-MM-DD"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """
    Fetch US Treasury yield-curve observations. Type A endpoint — cached 7 days.

    EXAMPLES
      ofclient treasury
      ofclient treasury --start 2025-07-01 --end 2025-07-31
    """
    from onefinance.cache.keys import make_key

    effective_no_cache = no_cache or _env_bool("OFCLIENT_NO_CACHE")
    effective_dry_run = dry_run or _env_bool("OFCLIENT_DRY_RUN")

    end_d = date.fromisoformat(end) if end else date.today()
    start_d = date.fromisoformat(start) if start else end_d - timedelta(days=30)

    if effective_dry_run:
        client = _make_client(config)
        key = make_key("treasury_rates", start=start_d, end=end_d)
        _dry_run_response("treasury", key, client)
        return

    try:
        client = _make_client(config)
        results = client.get_treasury_rates(
            start=start_d,
            end=end_d,
            no_cache=effective_no_cache,
            provider=provider,
        )
        data = [r.model_dump(mode="json") for r in results]
        source = results[0].source if results else "none"
        _emit(
            make_envelope(
                "treasury",
                data,
                {
                    "source": source,
                    "rows": len(data),
                    "start": start_d.isoformat(),
                    "end": end_d.isoformat(),
                },
            ),
            fmt,
        )
    except FinanceError as exc:
        _error_exit("treasury", exc)


@app.command()
def earnings_date(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
) -> None:
    """
    Return the next scheduled earnings date for a symbol.
    Scans the earnings calendar over the next 365 days and returns
    the first matching entry.

    EXAMPLES
      ofclient earnings-date AAPL
      ofclient earnings-date MSFT --format table
    """
    try:
        client = _make_client(config)
        today = date.today()
        entries = client.get_earnings_calendar(
            start=today,
            end=today + timedelta(days=365),
            symbol=symbol,
            no_cache=no_cache or _env_bool("OFCLIENT_NO_CACHE"),
            provider=provider,
        )
        if not entries:
            _error_exit(
                "earnings-date",
                NotSupportedError("earnings_calendar", symbol),
            )
            return
        next_entry = entries[0]
        data = {
            "symbol": symbol.upper(),
            "report_date": next_entry.report_date.isoformat(),
            "year": next_entry.year,
            "quarter": next_entry.quarter,
            "time_of_day": next_entry.time_of_day,
            "eps_estimate": next_entry.eps_estimate,
            "revenue_estimate": next_entry.revenue_estimate,
        }
        _emit(make_envelope("earnings_date", data, {"source": next_entry.source}), fmt)
    except FinanceError as exc:
        _error_exit("earnings-date", exc)


@app.command()
def estimates(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    no_cache: bool = typer.Option(False, "--no-cache", help=_HELP_NO_CACHE),
    provider: str | None = typer.Option(None, "--provider", help=_HELP_PROVIDER),
    fmt: str = typer.Option(
        os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", help=_HELP_FORMAT
    ),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_HELP_DRY_RUN),
) -> None:
    """Fetch forward-looking analyst estimates."""
    if dry_run or _env_bool("OFCLIENT_DRY_RUN"):
        from onefinance.cache.keys import make_key

        client = _make_client(config)
        key = make_key("estimates", symbol=symbol.upper())
        _dry_run_response("estimates", key, client)
        return

    try:
        client = _make_client(config)
        results = client.get_forward_estimates(symbol, no_cache=no_cache, provider=provider)
        data = [r.model_dump(mode="json") for r in results]
        # Get source from first result if any
        meta = {"source": results[0].source} if results else {}
        _emit(make_envelope("estimates", data, meta), fmt)
    except FinanceError as exc:
        _error_exit("estimates", exc)


# ---------------------------------------------------------------------------
# warm — prefetch watchlist into cache
# ---------------------------------------------------------------------------

_WARM_ENDPOINTS: set[str] = {"quote", "price", "indicators"}
_WARM_PRICE_RANGE = "1y"


@app.command()
def warm(
    symbols: list[str] = typer.Argument(..., help="One or more ticker symbols, e.g. AAPL MSFT SPY"),
    endpoints: str = typer.Option(
        "quote,price,indicators",
        "--endpoints",
        help="Comma-separated list of endpoints to warm. Supported: quote, price, indicators",
    ),
    range_: str = typer.Option(
        "1y", "--range", help="Date range for price bars: 1m|3m|6m|1y|2y|5y"
    ),
    workers: int = typer.Option(8, "--workers", help="Maximum concurrent provider calls"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Force refresh (bypass cache reads)"),
    config: str | None = typer.Option(
        os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
    ),
) -> None:
    """Prefetch a watchlist into the cache.

    Warms the cache for *symbols* by fetching quote, daily price history, and
    technical indicators concurrently.  Run before market open to ensure intraday
    calls hit the cache rather than paying provider latency.

    Examples:
        ofclient warm AAPL MSFT SPY
        ofclient warm AAPL TSLA NVDA --endpoints quote,price --range 6m
        ofclient warm AAPL --no-cache --endpoints price
    """
    import concurrent.futures
    import time as _time

    requested = {ep.strip().lower() for ep in endpoints.split(",")}
    unknown = requested - _WARM_ENDPOINTS
    if unknown:
        typer.echo(
            f"Unknown endpoints: {', '.join(sorted(unknown))}. "
            f"Supported: {', '.join(sorted(_WARM_ENDPOINTS))}",
            err=True,
        )
        raise typer.Exit(1)

    syms = [s.upper() for s in symbols]
    start_d, end_d = _resolve_dates(None, None, range_)

    client = _make_client(config)

    tasks: list[tuple[str, str]] = []
    for sym in syms:
        if "quote" in requested:
            tasks.append((sym, "quote"))
        if "price" in requested:
            tasks.append((sym, "price"))
        if "indicators" in requested:
            tasks.append((sym, "indicators"))

    def _fetch_one(sym: str, ep: str) -> tuple[str, str, bool, float, str | None]:
        t0 = _time.perf_counter()
        err_msg: str | None = None
        success = False
        try:
            if ep == "quote":
                client.get_quote(sym, no_cache=no_cache)
            elif ep == "price":
                client.get_price_history(sym, start_d, end_d, no_cache=no_cache)
            elif ep == "indicators":
                client.get_price_history(sym, start_d, end_d, no_cache=no_cache)
                if hasattr(client, "get_indicators"):
                    client.get_indicators(sym, start_d, end_d, no_cache=no_cache)
            success = True
        except Exception as exc:  # noqa: BLE001
            err_msg = str(exc)
        elapsed = round((_time.perf_counter() - t0) * 1000)
        return sym, ep, success, elapsed, err_msg

    total_start = _time.perf_counter()
    symbol_stats: dict[str, dict[str, Any]] = {}
    failed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(tasks) or 1)) as ex:
        futures = {ex.submit(_fetch_one, sym, ep): (sym, ep) for sym, ep in tasks}
        for fut in concurrent.futures.as_completed(futures):
            sym, ep, ok, ms, err = fut.result()
            if sym not in symbol_stats:
                symbol_stats[sym] = {"symbol": sym, "endpoints": {}}
            symbol_stats[sym]["endpoints"][ep] = {
                "ok": ok,
                "latency_ms": ms,
                **({"error": err} if err else {}),
            }
            if not ok:
                failed += 1

    total_ms = round((_time.perf_counter() - total_start) * 1000)
    summary = {
        "warmed": len(syms),
        "tasks": len(tasks),
        "failed": failed,
        "total_ms": total_ms,
        "endpoints_requested": sorted(requested),
        "symbols": list(symbol_stats.values()),
    }
    print_json(make_envelope("warm", summary, {}))


# ---------------------------------------------------------------------------
# M10 — capabilities and version
# ---------------------------------------------------------------------------


@app.command()
def capabilities() -> None:
    """Return machine-readable manifest of all commands and their arguments."""
    print_json(build_capabilities(app))


@app.command()
def version() -> None:
    """Return CLI version and schema version."""
    try:
        from importlib.metadata import version as pkg_version

        v = pkg_version("onefinance")
    except Exception:
        v = "0.1.0"
    print_json({"version": v, "schema_version": "1.0"})


register_admin_commands(
    app,
    cache_app=cache_app,
    providers_app=providers_app,
    config_app=config_app,
    make_client=lambda config: _make_client(config),
    emit=_emit,
    error_exit=_error_exit,
)

register_audit_commands(
    audit_app,
    make_client=lambda config: _make_client(config),
    emit=_emit,
    error_exit=_error_exit,
)
