"""OneFinance CLI — agent-first financial data access.

Default output is JSON. Use --format table for human-readable tables.
See design doc §16 for CLI design conventions.
"""

from __future__ import annotations

import os
from datetime import UTC, date, timedelta
from typing import Any

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
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    ttl: int | None = typer.Option(None, "--ttl"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
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
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
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
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """
    DESCRIPTION
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
                valid_data.append(res.model_dump(mode="json"))

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
    symbol: str = typer.Argument(...),
    statement: str = typer.Option("income", "--statement", help="income|balance|cashflow"),
    period: str = typer.Option("annual", "--period", help="annual|quarterly"),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Max number of periods to return (most recent first)"
    ),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
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
    symbol: str = typer.Argument(...),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
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


# ---------------------------------------------------------------------------
# insiders — Type A
# ---------------------------------------------------------------------------


@app.command()
def insiders(
    symbol: str = typer.Argument(...),
    since: str | None = typer.Option(None, "--since", help="Filter from date YYYY-MM-DD"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
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
    symbol: str = typer.Argument(...),
    period: str = typer.Option("annual", "--period", help="annual|quarterly"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass long-TTL cache, fetch latest"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
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
    symbol: str = typer.Argument(...),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass long-TTL cache, fetch latest"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
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
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    ttl: int | None = typer.Option(None, "--ttl"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """
    DESCRIPTION
      Compute a snapshot of technical indicators from daily OHLCV bars.
      Derived from the same data as `price`; shares the price_history cache.

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

      Volatility:
        atr14                          ATR(14), Wilder smoothing
        atr_pct                        atr14 / close * 100

      Volume:
        volume_ratio                   last volume / 5-day MA (excl. last bar)

      Levels:
        support_levels                 MAs below current close (high to low)
        resistance_levels              Recent 20-bar highs above close (low to high, top 3)

    DATA REQUIREMENTS
      Needs >=5 bars; MA20 needs >=20; MA60 needs >=60; MACD needs >=26;
      RSI14 and ATR14 need >=15. Default --range 6m covers all.

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
            "as_of": bars[-1].date.isoformat() if bars else None,
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
    symbol: str = typer.Argument(...),
    limit: int = typer.Option(20, "--limit", "-n"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
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
    symbol: str = typer.Argument(...),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
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
    symbol: str = typer.Argument(...),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
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
    symbol: str = typer.Argument(...),
    expiration: str | None = typer.Option(
        None,
        "--expiration",
        "-e",
        help="YYYY-MM-DD. If omitted, returns available expiration dates.",
    ),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Fetch options chain or available expiration dates."""
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
    symbol: str = typer.Argument(...),
    max_expirations: int = typer.Option(
        6, "--max-expirations", "-n", help="Max expiration dates to aggregate."
    ),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
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
def short_interest(
    symbol: str = typer.Argument(...),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
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
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
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
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
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
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
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
    symbol: str = typer.Argument(...),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
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
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """
    DESCRIPTION
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
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """
    DESCRIPTION
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
def earnings_date(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """
    DESCRIPTION
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
    symbol: str = typer.Argument(...),
    no_cache: bool = typer.Option(False, "--no-cache"),
    provider: str | None = typer.Option(None, "--provider"),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    dry_run: bool = typer.Option(False, "--dry-run"),
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
# M10 — capabilities and version
# ---------------------------------------------------------------------------

_CAPABILITIES: dict[str, Any] = {
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
                {
                    "name": "--range",
                    "required": False,
                    "type": "enum",
                    "allowed": ["1m", "3m", "6m", "1y", "2y", "5y"],
                },
                {"name": "--no-cache", "required": False, "type": "boolean", "default": False},
                {"name": "--provider", "required": False, "type": "string"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": [
                "ofclient price AAPL --range 1y",
                "ofclient price AAPL --start 2024-01-01",
            ],
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
            "name": "quotes",
            "description": (
                "Fetch current market quotes for multiple symbols as a batch. Type B — 30s TTL."
            ),
            "freshness_type": "B",
            "arguments": [
                {"name": "symbols", "required": True, "type": "list[string]"},
                {"name": "--no-cache", "required": False, "type": "boolean", "default": False},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient quotes AAPL MSFT"],
        },
        {
            "name": "financials",
            "description": "Fetch financial statements. Type A — 7-day TTL.",
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {
                    "name": "--statement",
                    "required": True,
                    "type": "enum",
                    "allowed": ["income", "balance", "cashflow"],
                },
                {
                    "name": "--period",
                    "required": True,
                    "type": "enum",
                    "allowed": ["annual", "quarterly"],
                },
                {
                    "name": "--limit",
                    "required": False,
                    "type": "integer",
                    "description": "Max periods to return (most recent first)",
                },
                {"name": "--no-cache", "required": False, "type": "boolean", "default": False},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": [
                "ofclient financials AAPL --statement income --period annual",
                "ofclient financials AAPL --statement income --period quarterly --limit 4",
            ],
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
                {
                    "name": "--period",
                    "required": True,
                    "type": "enum",
                    "allowed": ["annual", "quarterly"],
                },
                {"name": "--fresh", "required": False, "type": "boolean", "default": False},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": [
                "ofclient ratios AAPL --period annual",
                "ofclient ratios AAPL --period annual --fresh",
            ],
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
            "name": "indicators",
            "description": (
                "Compute a snapshot of technical indicators (moving averages, bias, "
                "trend, MACD, RSI, ATR, volume, support/resistance) from daily OHLCV "
                "bars. Type A — derived from the price_history cache."
            ),
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--start", "required": False, "type": "date", "format": "YYYY-MM-DD"},
                {"name": "--end", "required": False, "type": "date", "format": "YYYY-MM-DD"},
                {
                    "name": "--range",
                    "required": False,
                    "type": "enum",
                    "allowed": ["1m", "3m", "6m", "1y", "2y", "5y"],
                    "default": "6m",
                },
                {"name": "--no-cache", "required": False, "type": "boolean", "default": False},
                {"name": "--provider", "required": False, "type": "string"},
                {"name": "--ttl", "required": False, "type": "integer"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "indicators": [
                {"name": "ma5", "type": "float|null", "desc": "5-bar simple MA of close"},
                {"name": "ma10", "type": "float|null", "desc": "10-bar simple MA of close"},
                {"name": "ma20", "type": "float|null", "desc": "20-bar simple MA of close"},
                {"name": "ma60", "type": "float|null", "desc": "60-bar simple MA of close"},
                {"name": "bias_ma5", "type": "float|null", "desc": "(close - ma5)/ma5 * 100, in %"},
                {
                    "name": "bias_ma10",
                    "type": "float|null",
                    "desc": "(close - ma10)/ma10 * 100, in %",
                },
                {
                    "name": "bias_ma20",
                    "type": "float|null",
                    "desc": "(close - ma20)/ma20 * 100, in %",
                },
                {
                    "name": "bias_status",
                    "type": "enum",
                    "values": ["safe", "caution", "danger", "unknown"],
                    "desc": "safe: |bias_ma5|<2; caution: <5; danger: >=5",
                },
                {
                    "name": "ma_alignment",
                    "type": "enum",
                    "values": ["bullish", "bearish", "mixed", "unknown"],
                    "desc": "bullish: ma5>=ma10>=ma20; bearish: ma5<=ma10<=ma20",
                },
                {
                    "name": "trend_status",
                    "type": "enum",
                    "values": ["STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "STRONG_BEAR"],
                    "desc": "5-level trend from MA alignment + bias_ma5",
                },
                {"name": "macd_dif", "type": "float|null", "desc": "EMA12 - EMA26"},
                {
                    "name": "macd_dea",
                    "type": "float|null",
                    "desc": "EMA9 of macd_dif (signal line)",
                },
                {
                    "name": "macd_bar",
                    "type": "float|null",
                    "desc": "2 * (macd_dif - macd_dea) — histogram",
                },
                {
                    "name": "rsi14",
                    "type": "float|null",
                    "desc": "RSI(14), Wilder smoothing, range 0-100",
                },
                {
                    "name": "atr14",
                    "type": "float|null",
                    "desc": "ATR(14), Wilder smoothing — absolute price units",
                },
                {"name": "atr_pct", "type": "float|null", "desc": "atr14 / close * 100, in %"},
                {
                    "name": "volume_ratio",
                    "type": "float|null",
                    "desc": "last_volume / mean(last 5 volumes excl. current)",
                },
                {
                    "name": "support_levels",
                    "type": "list[float]",
                    "desc": "MA values below current close, high to low",
                },
                {
                    "name": "resistance_levels",
                    "type": "list[float]",
                    "desc": "Top 3 of last-20-bar highs above current close",
                },
            ],
            "examples": [
                "ofclient indicators AAPL",
                "ofclient indicators AAPL --range 1y",
                "ofclient indicators AAPL --format table",
            ],
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
            "name": "calendar",
            "description": (
                "Fetch earnings calendar for a date range. Returns scheduled earnings "
                "releases with EPS/revenue estimates. Type A — 4-hour TTL."
            ),
            "freshness_type": "A",
            "arguments": [
                {
                    "name": "--start",
                    "required": False,
                    "type": "date",
                    "format": "YYYY-MM-DD",
                    "default": "today",
                },
                {
                    "name": "--end",
                    "required": False,
                    "type": "date",
                    "format": "YYYY-MM-DD",
                    "default": "today+7d",
                },
                {
                    "name": "--symbol",
                    "required": False,
                    "type": "string",
                    "description": "Optional ticker filter",
                },
                {"name": "--no-cache", "required": False, "type": "boolean", "default": False},
                {"name": "--provider", "required": False, "type": "string"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": [
                "ofclient calendar",
                "ofclient calendar --start 2025-07-01 --end 2025-07-31",
                "ofclient calendar --symbol AAPL",
            ],
        },
        {
            "name": "earnings-date",
            "description": "Return next scheduled earnings date for a symbol. Scans calendar up to 365 days ahead.",  # noqa: E501
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--no-cache", "required": False, "type": "boolean", "default": False},
                {"name": "--provider", "required": False, "type": "string"},
            ],
            "examples": [
                "ofclient earnings-date AAPL",
                "ofclient earnings-date MSFT --format table",
            ],  # noqa: E501
        },
        {
            "name": "estimates",
            "description": "Fetch forward-looking analyst estimates. Type A — 4-hour TTL.",
            "freshness_type": "A",
            "arguments": [
                {"name": "symbol", "required": True, "type": "string"},
                {"name": "--no-cache", "required": False, "type": "boolean", "default": False},
                {"name": "--provider", "required": False, "type": "string"},
                {"name": "--dry-run", "required": False, "type": "boolean", "default": False},
            ],
            "examples": ["ofclient estimates AAPL"],
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
        {
            "name": "providers check",
            "description": (
                "Validate provider configuration (API keys, instantiation, "
                "tier membership) and optionally call each provider to verify "
                "API liveness. Always exits 0; failures appear in the JSON payload."
            ),
            "freshness_type": "B",
            "arguments": [
                {"name": "--ping", "required": False, "type": "boolean", "default": False},
                {"name": "--provider", "required": False, "type": "string"},
                {"name": "--ping-symbol", "required": False, "type": "string", "default": "AAPL"},
                {"name": "--ping-timeout", "required": False, "type": "number", "default": 5.0},
            ],
            "examples": [
                "ofclient providers check",
                "ofclient providers check --ping",
                "ofclient providers check --provider fmp --ping",
            ],
        },
    ],
}


@app.command()
def capabilities() -> None:
    """Return machine-readable manifest of all commands and their arguments."""
    print_json(_CAPABILITIES)


@app.command()
def version() -> None:
    """Return CLI version and schema version."""
    try:
        from importlib.metadata import version as pkg_version

        v = pkg_version("onefinance")
    except Exception:
        v = "0.1.0"
    print_json({"version": v, "schema_version": "1.0"})


@app.command()
def doctor(
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """
    DESCRIPTION
      Check configuration for common setup issues and suggest fixes.

      Inspects API keys, tier lists, fallback_order, config file validity,
      cache directory access, and active environment variables.  Never makes
      live API calls — entirely offline.

    EXIT CODE
      0 when healthy (no errors found).
      1 when one or more errors are found (warnings/info do not trigger exit 1).

    EXAMPLES
      ofclient doctor
      ofclient doctor --format table
    """
    try:
        client = _make_client(config)
        report = client.doctor(config_path=config)
        client.close()
    except Exception as exc:
        print_json(
            {
                "healthy": False,
                "summary": {"errors": 1, "warnings": 0, "info": 0},
                "findings": [
                    {
                        "level": "error",
                        "check": "client_init",
                        "message": f"Failed to initialise client: {exc}",
                        "suggestion": "Check your config file or API key environment variables",
                    }
                ],
            }
        )
        raise typer.Exit(1)

    if fmt == "table":
        findings = report.get("findings", [])
        rows = [
            {
                "level": f["level"].upper(),
                "check": f["check"],
                "message": f["message"],
                "suggestion": f["suggestion"],
            }
            for f in findings
        ]
        summary = report["summary"]
        _emit(
            make_envelope(
                "doctor",
                {
                    "healthy": report["healthy"],
                    "active_providers": report.get("active_providers", []),
                    "fallback_order": report.get("fallback_order", []),
                    **summary,
                },
                {},
            ),
            "json",
        )
        _emit(make_envelope("doctor findings", rows, {"rows": len(rows)}), "table")
    else:
        print_json(report)

    if not report.get("healthy", True):
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# M10 — cache, providers, config sub-commands
# ---------------------------------------------------------------------------


@cache_app.command("stats")
def cache_stats(
    days: int = typer.Option(1, "--days", help="Audit window for provider usage stats."),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format", "-f"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """Show cache statistics: entry count, size, hit rate, and per-provider API usage."""
    from datetime import datetime, timedelta

    try:
        client = _make_client(config)
        cache_st = client.cache.stats()
        since = datetime.now(UTC) - timedelta(days=days)
        audit_st = client.audit_stats(since=since)
        data = {
            "entries": cache_st["entries"],
            "size_mb": cache_st["size_mb"],
            "size_limit_bytes": cache_st["size_limit_bytes"],
            "hits": cache_st["hits"],
            "misses": cache_st["misses"],
            "hit_rate": f"{cache_st['hit_rate']:.1%}",
            "provider_usage": {
                "period_days": days,
                "total_api_calls": audit_st.total_calls,
                "cache_hits": audit_st.cache_hits,
                "cache_hit_rate": f"{audit_st.cache_hit_rate:.1%}",
                "stale_serves": audit_st.stale_serves,
                "stale_serve_rate": f"{audit_st.stale_serve_rate:.1%}",
                "calls_by_provider": audit_st.calls_by_provider,
                "errors_by_provider": audit_st.errors_by_provider,
                "avg_latency_ms_by_provider": audit_st.avg_latency_ms_by_provider,
                "latency_p95_ms_by_provider": audit_st.latency_p95_ms_by_provider,
            },
        }
        if fmt == "table":
            rows = []
            all_provs = sorted(
                set(
                    list(audit_st.calls_by_provider.keys())
                    + list(audit_st.errors_by_provider.keys())
                )
            )
            for p in all_provs:
                rows.append(
                    {
                        "provider": p,
                        "calls": audit_st.calls_by_provider.get(p, 0),
                        "errors": audit_st.errors_by_provider.get(p, 0),
                        "avg_latency_ms": audit_st.avg_latency_ms_by_provider.get(p, 0),
                    }
                )
            summary = {
                "entries": cache_st["entries"],
                "size_mb": cache_st["size_mb"],
                "hit_rate": f"{cache_st['hit_rate']:.1%}",
                "cache_hit_rate_24h": f"{audit_st.cache_hit_rate:.1%}",
            }
            _emit(make_envelope("cache stats summary", summary, {}), "json")
            _emit(make_envelope("cache stats providers", rows, {"rows": len(rows)}), "table")
        else:
            print_json(data)
        client.close()
    except FinanceError as exc:
        _error_exit("cache stats", exc)


@providers_app.command("status")
def providers_status(
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """Show per-provider cooldown and health state."""
    try:
        client = _make_client(config)
        state = client.providers.state()
        print_json(state)
    except FinanceError as exc:
        _error_exit("providers status", exc)


@providers_app.command("check")
def providers_check(
    ping: bool = typer.Option(
        False,
        "--ping",
        help="Also call each provider's get_quote() to verify the API is live",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Restrict the check to a single provider",
    ),
    ping_symbol: str = typer.Option(
        "AAPL",
        "--ping-symbol",
        help="Symbol used for the ping call",
    ),
    ping_timeout_s: float = typer.Option(
        5.0,
        "--ping-timeout",
        help="Ping timeout in seconds (informational)",
    ),
    fmt: str = typer.Option(os.environ.get("OFCLIENT_OUTPUT", "json"), "--format"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """
    DESCRIPTION
      Check provider configuration and (optionally) API reachability.

      For every provider declared in the config, reports:
        - api_key_present:  is the API key env var set?
        - instantiable:     could the provider class be constructed?
        - in_use_in_tier:   is the provider referenced in any tier table?
        - tier_endpoints:   which endpoints route to this provider
        - status:           ok | missing_api_key | not_instantiable |
                            unused | ping_failed

      With --ping, also calls get_quote(--ping-symbol) on each
      instantiated provider and reports latency + error code.

      Also reports `tier_issues` for any tier list that references a
      provider name not declared in the providers section of the config.

    EXIT CODE
      Always 0. Failures appear in the JSON payload.

    EXAMPLES
      ofclient providers check
      ofclient providers check --ping
      ofclient providers check --provider fmp --ping
      ofclient providers check --ping --ping-symbol MSFT
    """
    try:
        client = _make_client(config)
        report = client.check_providers(
            ping=ping,
            only=provider,
            ping_symbol=ping_symbol,
            ping_timeout_s=ping_timeout_s,
        )
        envelope = make_envelope(
            "providers check",
            report,
            {
                "pings_attempted": ping,
                "total": report["summary"]["total"],
            },
        )
        _emit(envelope, fmt)
    except FinanceError as exc:
        _error_exit("providers check", exc)


@config_app.command("show")
def config_show(
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """Print current configuration (tiers, cache settings, providers)."""
    try:
        client = _make_client(config)
        cfg = client._config
        print_json(
            {
                "tiers": cfg.tiers,
                "cache": {"dir": cfg.cache.dir, "size_limit_gb": cfg.cache.size_limit_gb},
                "cooldown": {
                    "default_initial_s": cfg.cooldown.default_initial_s,
                    "max_backoff_s": cfg.cooldown.max_backoff_s,
                },
            }
        )
    except Exception as exc:
        print_json({"error": str(exc)})
        raise typer.Exit(1)


@config_app.command("init")
def config_init(
    output: str | None = typer.Option(None, "--output", help="Path to write config"),
) -> None:
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
  alpha_vantage:
    api_key_env: ALPHAVANTAGE_API_KEY
    timeout_s: 10
  massive:
    api_key_env: MASSIVE_API_KEY
    timeout_s: 10
  tradier:
    api_key_env: TRADIER_TOKEN
    timeout_s: 10
  edgar:
    timeout_s: 15

tiers:
  price_history: [yfinance, fmp, twelve_data, massive, alpha_vantage, finnhub]
  financials: [edgar, fmp, finnhub, alpha_vantage, yfinance]
  info: [fmp, finnhub, massive, alpha_vantage, yfinance]
  insider_trades: [fmp, finnhub]
  quote: [finnhub, yfinance, massive, alpha_vantage, fmp]
  ratios:
    default: [fmp, finnhub]
    fresh: [fmp, finnhub]
  earnings:
    default: [fmp, finnhub, alpha_vantage]
    fresh: [fmp, finnhub]
  news: [yfinance, fmp, massive, alpha_vantage]
  corporate_actions: [fmp, massive, finnhub, yfinance]
  options_expirations: [tradier, yfinance, massive]
  option_chain: [tradier, yfinance, massive]
  earnings_calendar: [finnhub, fmp]
  peers: [fmp, finnhub]

cache:
  dir: ~/.one_finance_data/cache
  size_limit_gb: 2

cooldown:
  default_initial_s: 60
  max_backoff_s: 3600
"""
    dest = (
        pathlib.Path(output).expanduser()
        if output
        else pathlib.Path("~/.one_finance_data/config.yaml").expanduser()
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(template)
    print_json({"status": "created", "path": str(dest)})


# ---------------------------------------------------------------------------
# Audit sub-commands
# ---------------------------------------------------------------------------


@audit_app.command("stats")
def audit_stats(
    days: int = typer.Option(1, "--days", help="Number of days to aggregate."),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Show aggregate API call stats: calls per provider, errors, latency, cache hit rate."""
    from datetime import datetime, timedelta

    try:
        client = _make_client(config)
        since = datetime.now(UTC) - timedelta(days=days)
        stats = client.audit_stats(since=since)
        data = {
            "period_days": days,
            "total_api_calls": stats.total_calls,
            "cache_hits": stats.cache_hits,
            "cache_hit_rate": f"{stats.cache_hit_rate:.1%}",
            "stale_serves": stats.stale_serves,
            "stale_serve_rate": f"{stats.stale_serve_rate:.1%}",
            "avg_stale_age_s": stats.avg_stale_age_s,
            "max_stale_age_s": stats.max_stale_age_s,
            "fallback_requests": stats.fallback_requests,
            "fallback_rate": f"{stats.fallback_rate:.1%}",
            "calls_by_provider": stats.calls_by_provider,
            "errors_by_provider": stats.errors_by_provider,
            "not_supported_by_provider": stats.not_supported_by_provider,
            "primary_failures_by_provider": stats.primary_failures_by_provider,
            "fallback_success_by_provider": stats.fallback_success_by_provider,
            "fallback_failure_by_provider": stats.fallback_failure_by_provider,
            "rate_limits_by_provider": stats.rate_limits_by_provider,
            "avg_latency_ms_by_provider": stats.avg_latency_ms_by_provider,
            "latency_p50_ms_by_provider": stats.latency_p50_ms_by_provider,
            "latency_p95_ms_by_provider": stats.latency_p95_ms_by_provider,
            "latency_p99_ms_by_provider": stats.latency_p99_ms_by_provider,
            "calls_by_endpoint": stats.calls_by_endpoint,
            "errors_by_endpoint": stats.errors_by_endpoint,
        }
        if fmt == "table":
            all_provs = sorted(
                set(
                    list(stats.calls_by_provider.keys())
                    + list(stats.errors_by_provider.keys())
                    + list(stats.not_supported_by_provider.keys())
                )
            )
            prov_rows = [
                {
                    "provider": p,
                    "calls": stats.calls_by_provider.get(p, 0),
                    "errors": stats.errors_by_provider.get(p, 0),
                    "not_supported": stats.not_supported_by_provider.get(p, 0),
                    "prim_fail": stats.primary_failures_by_provider.get(p, 0),
                    "fb_ok": stats.fallback_success_by_provider.get(p, 0),
                    "fb_fail": stats.fallback_failure_by_provider.get(p, 0),
                    "rate_limits": stats.rate_limits_by_provider.get(p, 0),
                    "latency_ms": stats.avg_latency_ms_by_provider.get(p, 0),
                    "p95_ms": stats.latency_p95_ms_by_provider.get(p, 0),
                }
                for p in all_provs
            ]
            all_endpoints = sorted(
                set(list(stats.calls_by_endpoint.keys()) + list(stats.errors_by_endpoint.keys()))
            )
            endpoint_rows = [
                {
                    "endpoint": e,
                    "calls": stats.calls_by_endpoint.get(e, 0),
                    "errors": stats.errors_by_endpoint.get(e, 0),
                }
                for e in all_endpoints
            ]
            summary = {
                "period_days": days,
                "total_api_calls": stats.total_calls,
                "cache_hit_rate": f"{stats.cache_hit_rate:.1%}",
                "stale_serves": stats.stale_serves,
                "stale_serve_rate": f"{stats.stale_serve_rate:.1%}",
                "max_stale_age_s": stats.max_stale_age_s,
                "fallback_requests": stats.fallback_requests,
                "fallback_rate": f"{stats.fallback_rate:.1%}",
            }
            _emit(make_envelope("audit stats summary", summary, {}), "json")
            _emit(
                make_envelope("audit stats by provider", prov_rows, {"rows": len(prov_rows)}),
                "table",
            )
            _emit(
                make_envelope(
                    "audit stats by endpoint", endpoint_rows, {"rows": len(endpoint_rows)}
                ),
                "table",
            )
        else:
            print_json(data)
        client.close()
    except FinanceError as exc:
        _error_exit("audit stats", exc)


@audit_app.command("recent")
def audit_recent(
    provider: str | None = typer.Option(None, "--provider", "-p"),
    endpoint: str | None = typer.Option(None, "--endpoint", "-e"),
    status: str | None = typer.Option(None, "--status", "-s"),
    limit: int = typer.Option(20, "--limit", "-n"),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Show recent audit log entries (newest first)."""
    try:
        client = _make_client(config)
        entries = client.audit_log.query(
            provider=provider,
            endpoint=endpoint,
            status=status,
            limit=limit,
        )
        if fmt == "table":
            rows: list[dict[str, Any]] = []
            for e in entries:
                ts = e.timestamp.isoformat()
                time_part = ts.split("T")[1][:8] if "T" in ts else ts
                tier = f"{e.tier_position + 1}/{e.tier_total}"
                rows.append(
                    {
                        "time": time_part,
                        "endpoint": e.endpoint,
                        "provider": e.provider,
                        "symbol": e.symbol or "-",
                        "status": e.status,
                        "ms": round(e.latency_ms),
                        "tier": tier,
                        "fallback": "Y" if e.is_fallback else "N",
                        "error": e.error_code or "",
                    }
                )
        else:
            rows = [e.to_dict() for e in entries]
        _emit(make_envelope("audit recent", rows, {"rows": len(rows)}), fmt)
        client.close()
    except FinanceError as exc:
        _error_exit("audit recent", exc)


@audit_app.command("path")
def audit_path(
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """Print the audit log file path."""
    try:
        client = _make_client(config)
        path = client.audit_log.path
        print_json({"path": str(path) if path else None, "enabled": client.audit_log.enabled})
        client.close()
    except FinanceError as exc:
        _error_exit("audit path", exc)


@audit_app.command("truncate")
def audit_truncate(
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Required: acknowledge that all audit entries will be permanently deleted.",
    ),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """Truncate (clear) the audit log.

    All recorded entries are permanently deleted.  Pass --confirm to proceed.

    Example:
      ofclient audit truncate --confirm
    """
    if not confirm:
        typer.echo(
            "Aborted. Pass --confirm to permanently delete all audit log entries.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        client = _make_client(config)
        path = client.audit_log.path
        client.audit_log.clear()
        client.close()
        print_json({"status": "truncated", "path": str(path) if path else None})
    except FinanceError as exc:
        _error_exit("audit truncate", exc)


@audit_app.command("follow")
def audit_follow(
    provider: str | None = typer.Option(None, "--provider", "-p", help="Filter by provider name."),
    endpoint: str | None = typer.Option(None, "--endpoint", "-e", help="Filter by endpoint name."),
    status: str | None = typer.Option(
        None, "--status", "-s", help="Filter by status (e.g. error, cache_hit)."
    ),
    symbol: str | None = typer.Option(
        None, "--symbol", help="Filter by ticker symbol (case-insensitive)."
    ),
    interval: float = typer.Option(0.5, "--interval", "-i", help="Poll interval in seconds."),
    config: str | None = typer.Option(os.environ.get("OFCLIENT_CONFIG"), "--config"),
) -> None:
    """Stream new audit log entries as they arrive (like tail -f).

    Prints one JSON object per line to stdout.  Press Ctrl-C to stop.

    Filters (--provider, --endpoint, --status, --symbol) are applied to every
    new line that arrives, so you can watch only the traffic you care about.

    Examples:
      ofclient audit follow
      ofclient audit follow --status error
      ofclient audit follow --provider fmp --endpoint quote
      ofclient audit follow --symbol AAPL
    """
    import json
    import time

    try:
        client = _make_client(config)
        log_path = client.audit_log.path
        client.close()

        if log_path is None or not client.audit_log.enabled:
            typer.echo("Audit log is disabled or path is not set.", err=True)
            raise typer.Exit(1)

        # Seed the starting position at the current end of file so we only
        # show *new* entries written after this command starts.
        try:
            initial_size = log_path.stat().st_size if log_path.exists() else 0
        except OSError:
            initial_size = 0

        typer.echo(
            f"Following {log_path}  (Ctrl-C to stop)",
            err=True,
        )

        byte_offset = initial_size

        while True:
            try:
                if not log_path.exists():
                    time.sleep(interval)
                    continue

                current_size = log_path.stat().st_size

                if current_size < byte_offset:
                    # File was truncated / rotated — reset to start.
                    byte_offset = 0

                if current_size == byte_offset:
                    time.sleep(interval)
                    continue

                with open(log_path, "rb") as fh:
                    fh.seek(byte_offset)
                    new_bytes = fh.read()
                    byte_offset = fh.tell()

                lines = new_bytes.decode("utf-8", errors="replace").splitlines()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Apply filters
                    if provider and obj.get("provider") != provider:
                        continue
                    if endpoint and obj.get("endpoint") != endpoint:
                        continue
                    if status and obj.get("status") != status:
                        continue
                    if symbol and (obj.get("symbol") or "").upper() != symbol.upper():
                        continue

                    # Emit one compact JSON line
                    typer.echo(json.dumps(obj, separators=(",", ":")))

                time.sleep(interval)

            except KeyboardInterrupt:
                typer.echo("\nStopped.", err=True)
                raise typer.Exit(0)

    except FinanceError as exc:
        _error_exit("audit follow", exc)
