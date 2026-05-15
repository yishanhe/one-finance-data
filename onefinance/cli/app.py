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


app = typer.Typer(
    name="ofclient",
    help="OneFinance CLI — unified financial data access for agents and humans.",
    add_completion=False,
    invoke_without_command=True,
    callback=_banner_callback,
)

config_app = typer.Typer(name="config", help="Configuration commands.")
cache_app = typer.Typer(name="cache", help="Cache inspection commands.")
providers_app = typer.Typer(name="providers", help="Provider status commands.")
audit_app = typer.Typer(name="audit", help="Audit log commands.")

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
# Alternative Data
# ---------------------------------------------------------------------------

@app.command()
def news(
    symbol: str = typer.Argument(...),
    limit: int = typer.Option(20, "--limit", "-n"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
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
    symbol: str = typer.Argument(...),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
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
    symbol: str = typer.Argument(...),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
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
    symbol: str = typer.Argument(...),
    expiration: Optional[str] = typer.Option(None, "--expiration", "-e", help="YYYY-MM-DD. If omitted, returns available expiration dates."),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Fetch options chain or available expiration dates."""
    if dry_run or _env_bool("OFCLIENT_DRY_RUN"):
        from onefinance.cache.keys import make_key
        client = _make_client(config)
        if not expiration:
            key = make_key("options_expirations", symbol=symbol.upper())
            _dry_run_response("options", key, client)
        else:
            try:
                exp_d = datetime.date.fromisoformat(expiration)
            except ValueError:
                _error_exit("options", ValueError(f"Invalid expiration date: {expiration}. Use YYYY-MM-DD."))
            key = make_key("option_chain", symbol=symbol.upper(), expiration=exp_d)
            _dry_run_response("options", key, client)
        return

    try:
        client = _make_client(config)
        if not expiration:
            dates = client.get_options_expirations(symbol, no_cache=no_cache, provider=provider)
            data = [d.isoformat() for d in dates]
            _emit(make_envelope("options_expirations", data, {"symbol": symbol.upper(), "count": len(dates)}), fmt)
        else:
            try:
                exp_d = datetime.date.fromisoformat(expiration)
            except ValueError:
                _error_exit("options", ValueError(f"Invalid expiration date: {expiration}. Use YYYY-MM-DD."))
                return
            chain = client.get_option_chain(symbol, exp_d, no_cache=no_cache, provider=provider)
            data = chain.model_dump(mode="json")
            _emit(make_envelope("option_chain", data, {"source": chain.source, "calls": len(chain.calls), "puts": len(chain.puts)}), fmt)
    except FinanceError as exc:
        _error_exit("options", exc)

@app.command()
def screen(
    query: str = typer.Argument(..., help="Query string for screener (e.g. 'marketCapMoreThan=1000000000')"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Screen stocks based on a query."""
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
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
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
        _emit(make_envelope("sector_overview", data, {"source": result.source, "companies": len(result.top_companies or [])}), fmt)
    except FinanceError as exc:
        _error_exit("sector", exc)

@app.command()
def analyst(
    symbol: str = typer.Argument(...),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: Optional[str] = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
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
        {
            "name": "news",
            "description": "Fetch recent news articles.",
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--limit", "required": False, "type": "integer", "default": 20},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient news AAPL"],
        },
        {
            "name": "actions",
            "description": "Fetch corporate actions (dividends and splits).",
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient actions AAPL"],
        },
        {
            "name": "holders",
            "description": "Fetch top institutional holders.",
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient holders AAPL"],
        },
        {
            "name": "analyst",
            "description": "Fetch analyst price targets and ratings.",
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient analyst AAPL"],
        },
        {
            "name": "options",
            "description": "Fetch options chain or available expiration dates.",
            "freshness_type": "B",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--expiration", "required": False, "type": "string"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient options AAPL", "ofclient options AAPL --expiration 2024-01-19"],
        },
        {
            "name": "screen",
            "description": "Screen stocks based on a query.",
            "freshness_type": "B",
            "arguments": [
                {"name": "query", "required": True, "type": "string"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient screen 'marketCapMoreThan=1000000000'"],
        },
        {
            "name": "sector",
            "description": "Fetch sector overview.",
            "freshness_type": "B",
            "arguments": [
                {"name": "name", "required": True, "type": "string"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient sector technology"],
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
  dir: ~/.one_finance_data/cache
  size_limit_gb: 2

cooldown:
  default_initial_s: 60
  max_backoff_s: 3600
"""
    dest = pathlib.Path(output).expanduser() if output else pathlib.Path("~/.one_finance_data/config.yaml").expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(template)
    print_json({"status": "created", "path": str(dest)})


# ---------------------------------------------------------------------------
# Audit sub-commands
# ---------------------------------------------------------------------------

@audit_app.command("stats")
def audit_stats(
    days: int = typer.Option(1, "--days", help="Number of days to aggregate."),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    fmt: str = typer.Option("json", "--format", "-f"),
):
    """Show aggregate API call stats: calls per provider, errors, latency, cache hit rate."""
    from datetime import datetime, timedelta, timezone
    try:
        client = _make_client(config)
        since = datetime.now(timezone.utc) - timedelta(days=days)
        stats = client.audit_stats(since=since)
        data = {
            "period_days": days,
            "total_api_calls": stats.total_calls,
            "cache_hits": stats.cache_hits,
            "cache_hit_rate": f"{stats.cache_hit_rate:.1%}",
            "calls_by_provider": stats.calls_by_provider,
            "errors_by_provider": stats.errors_by_provider,
            "rate_limits_by_provider": stats.rate_limits_by_provider,
            "avg_latency_ms_by_provider": stats.avg_latency_ms_by_provider,
        }
        if fmt == "table":
            # Build rows for table display
            rows = []
            all_provs = sorted(
                set(list(stats.calls_by_provider.keys()) +
                    list(stats.errors_by_provider.keys()))
            )
            for p in all_provs:
                rows.append({
                    "provider": p,
                    "calls": stats.calls_by_provider.get(p, 0),
                    "errors": stats.errors_by_provider.get(p, 0),
                    "rate_limits": stats.rate_limits_by_provider.get(p, 0),
                    "avg_latency_ms": stats.avg_latency_ms_by_provider.get(p, 0),
                })
            _emit(make_envelope("audit stats", rows, {"rows": len(rows)}), fmt)
        else:
            print_json(data)
        client.close()
    except FinanceError as exc:
        _error_exit("audit stats", exc)


@audit_app.command("recent")
def audit_recent(
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
    endpoint: Optional[str] = typer.Option(None, "--endpoint", "-e"),
    status: Optional[str] = typer.Option(None, "--status", "-s"),
    limit: int = typer.Option(20, "--limit", "-n"),
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    fmt: str = typer.Option("json", "--format", "-f"),
):
    """Show recent audit log entries (newest first)."""
    try:
        client = _make_client(config)
        entries = client.audit_log.query(
            provider=provider,
            endpoint=endpoint,
            status=status,
            limit=limit,
        )
        rows = [e.to_dict() for e in entries]
        _emit(make_envelope("audit recent", rows, {"rows": len(rows)}), fmt)
        client.close()
    except FinanceError as exc:
        _error_exit("audit recent", exc)


@audit_app.command("path")
def audit_path(
    config: Optional[str] = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
):
    """Print the audit log file path."""
    try:
        client = _make_client(config)
        path = client.audit_log.path
        print_json({"path": str(path) if path else None, "enabled": client.audit_log.enabled})
        client.close()
    except FinanceError as exc:
        _error_exit("audit path", exc)
