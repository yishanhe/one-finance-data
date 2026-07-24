"""Registration for operational CLI commands and command groups."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from onefinance.cli._audit_views import cache_provider_rows
from onefinance.cli.format import make_envelope, print_json
from onefinance.core.client import OneFinanceClient
from onefinance.core.config import default_config_template
from onefinance.core.errors import FinanceError

ClientFactory = Callable[[str | None], OneFinanceClient]
Emitter = Callable[[dict[str, Any], str], None]
ErrorHandler = Callable[[str, FinanceError], None]

_HELP_FORMAT = "Output format: json, table, or csv. Default json (env: OFCLIENT_OUTPUT)."
_HELP_CONFIG = "Path to a YAML config file (env: OFCLIENT_CONFIG)."


def register_admin_commands(
    app: typer.Typer,
    *,
    cache_app: typer.Typer,
    providers_app: typer.Typer,
    config_app: typer.Typer,
    make_client: ClientFactory,
    emit: Emitter,
    error_exit: ErrorHandler,
) -> None:
    """Attach doctor, cache, provider, and config commands."""

    @app.command()
    def doctor(
        fmt: str = typer.Option(
            os.environ.get("OFCLIENT_OUTPUT", "json"),
            "--format",
            help=_HELP_FORMAT,
        ),
        config: str | None = typer.Option(
            os.environ.get("OFCLIENT_CONFIG"),
            "--config",
            help=_HELP_CONFIG,
        ),
    ) -> None:
        """
        Check configuration for common setup issues and suggest fixes.

          Inspects API keys, tier lists, fallback_order, config file validity,
          cache directory access, and active environment variables. Never makes
          live API calls — entirely offline.

        EXIT CODE
          0 when healthy (no errors found).
          1 when one or more errors are found (warnings/info do not trigger exit 1).

        EXAMPLES
          ofclient doctor
          ofclient doctor --format table
        """
        try:
            client = make_client(config)
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
                            "suggestion": (
                                "Check your config file or API key environment variables"
                            ),
                        }
                    ],
                }
            )
            raise typer.Exit(1)

        if fmt == "table":
            findings = report.get("findings", [])
            rows = [
                {
                    "level": finding["level"].upper(),
                    "check": finding["check"],
                    "message": finding["message"],
                    "suggestion": finding["suggestion"],
                }
                for finding in findings
            ]
            summary = report["summary"]
            emit(
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
            emit(
                make_envelope("doctor findings", rows, {"rows": len(rows)}),
                "table",
            )
        else:
            print_json(report)

        if not report.get("healthy", True):
            raise typer.Exit(1)

    @cache_app.command("stats")
    def cache_stats(
        days: int = typer.Option(1, "--days", help="Audit window for provider usage stats."),
        fmt: str = typer.Option(
            os.environ.get("OFCLIENT_OUTPUT", "json"),
            "--format",
            "-f",
            help=_HELP_FORMAT,
        ),
        config: str | None = typer.Option(
            os.environ.get("OFCLIENT_CONFIG"),
            "--config",
            help=_HELP_CONFIG,
        ),
    ) -> None:
        """Show cache size, hit rate, and per-provider API usage."""
        try:
            client = make_client(config)
            cache_stats = client.cache.stats()
            since = datetime.now(UTC) - timedelta(days=days)
            audit_stats = client.audit_stats(since=since)
            data = {
                "entries": cache_stats["entries"],
                "size_mb": cache_stats["size_mb"],
                "size_limit_bytes": cache_stats["size_limit_bytes"],
                "hits": cache_stats["hits"],
                "misses": cache_stats["misses"],
                "hit_rate": f"{cache_stats['hit_rate']:.1%}",
                "provider_usage": {
                    "period_days": days,
                    "total_api_calls": audit_stats.total_calls,
                    "cache_hits": audit_stats.cache_hits,
                    "cache_hit_rate": f"{audit_stats.cache_hit_rate:.1%}",
                    "stale_serves": audit_stats.stale_serves,
                    "stale_serve_rate": f"{audit_stats.stale_serve_rate:.1%}",
                    "calls_by_provider": audit_stats.calls_by_provider,
                    "errors_by_provider": audit_stats.errors_by_provider,
                    "avg_latency_ms_by_provider": audit_stats.avg_latency_ms_by_provider,
                    "latency_p95_ms_by_provider": audit_stats.latency_p95_ms_by_provider,
                },
            }
            if fmt == "table":
                rows = cache_provider_rows(audit_stats)
                summary = {
                    "entries": cache_stats["entries"],
                    "size_mb": cache_stats["size_mb"],
                    "hit_rate": f"{cache_stats['hit_rate']:.1%}",
                    "cache_hit_rate_24h": f"{audit_stats.cache_hit_rate:.1%}",
                }
                emit(make_envelope("cache stats summary", summary, {}), "json")
                emit(
                    make_envelope(
                        "cache stats providers",
                        rows,
                        {"rows": len(rows)},
                    ),
                    "table",
                )
            else:
                print_json(data)
            client.close()
        except FinanceError as exc:
            error_exit("cache stats", exc)

    @providers_app.command("status")
    def providers_status(
        config: str | None = typer.Option(
            os.environ.get("OFCLIENT_CONFIG"),
            "--config",
            help=_HELP_CONFIG,
        ),
    ) -> None:
        """Show per-provider cooldown and health state."""
        try:
            client = make_client(config)
            print_json(client.providers.state())
        except FinanceError as exc:
            error_exit("providers status", exc)

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
        fmt: str = typer.Option(
            os.environ.get("OFCLIENT_OUTPUT", "json"),
            "--format",
            help=_HELP_FORMAT,
        ),
        config: str | None = typer.Option(
            os.environ.get("OFCLIENT_CONFIG"),
            "--config",
            help=_HELP_CONFIG,
        ),
    ) -> None:
        """
        Check provider configuration and (optionally) API reachability.

          For every provider declared in the config, reports API-key presence,
          instantiation, tier use, plan-gated endpoints, and health status.

          With --ping, also calls get_quote(--ping-symbol) on each instantiated
          provider and reports latency plus a stable error code.

          Also reports tier_issues for tier entries referencing undeclared providers.

        EXIT CODE
          Always 0. Failures appear in the JSON payload.

        EXAMPLES
          ofclient providers check
          ofclient providers check --ping
          ofclient providers check --provider fmp --ping
          ofclient providers check --ping --ping-symbol MSFT
        """
        try:
            client = make_client(config)
            report = client.check_providers(
                ping=ping,
                only=provider,
                ping_symbol=ping_symbol,
                ping_timeout_s=ping_timeout_s,
            )
            emit(
                make_envelope(
                    "providers check",
                    report,
                    {
                        "pings_attempted": ping,
                        "total": report["summary"]["total"],
                    },
                ),
                fmt,
            )
        except FinanceError as exc:
            error_exit("providers check", exc)

    @config_app.command("show")
    def config_show(
        config: str | None = typer.Option(
            os.environ.get("OFCLIENT_CONFIG"),
            "--config",
            help=_HELP_CONFIG,
        ),
    ) -> None:
        """Print the resolved tiers, cache settings, and cooldown settings."""
        try:
            client = make_client(config)
            resolved = client.config
            print_json(
                {
                    "tiers": resolved.tiers,
                    "cache": {
                        "dir": resolved.cache.dir,
                        "size_limit_gb": resolved.cache.size_limit_gb,
                    },
                    "cooldown": {
                        "default_initial_s": resolved.cooldown.default_initial_s,
                        "max_backoff_s": resolved.cooldown.max_backoff_s,
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
        destination = (
            Path(output).expanduser()
            if output
            else Path("~/.one_finance_data/config.yaml").expanduser()
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(default_config_template())
        print_json({"status": "created", "path": str(destination)})
