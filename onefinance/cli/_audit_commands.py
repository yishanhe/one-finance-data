"""Registration for the ``ofclient audit`` command group."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from onefinance.audit.log import AuditLog
from onefinance.cli._audit_views import (
    audit_endpoint_rows,
    audit_provider_rows,
    audit_stats_json,
    audit_stats_summary,
)
from onefinance.cli.format import make_envelope, print_json
from onefinance.core.errors import FinanceError

Emitter = Callable[[dict[str, Any], str], None]
ErrorHandler = Callable[[str, FinanceError], None]

_HELP_FORMAT = "Output format: json, table, or csv. Default json (env: OFCLIENT_OUTPUT)."
_HELP_CONFIG = "Path to a YAML config file (env: OFCLIENT_CONFIG)."


def register_audit_commands(
    audit_app: typer.Typer,
    *,
    emit: Emitter,
    error_exit: ErrorHandler,
) -> None:
    """Attach all audit commands to a Typer group."""

    @audit_app.command("stats")
    def audit_stats(
        days: int = typer.Option(1, "--days", help="Number of days to aggregate."),
        config: str | None = typer.Option(
            os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
        ),
        fmt: str = typer.Option("json", "--format", "-f", help=_HELP_FORMAT),
    ) -> None:
        """Show aggregate API call stats: calls, errors, latency, and cache hit rate."""
        try:
            log = AuditLog(retention_days=0)
            since = datetime.now(UTC) - timedelta(days=days)
            stats = log.stats(since=since)
            data = audit_stats_json(stats, period_days=days)
            if fmt == "table":
                provider_rows = audit_provider_rows(stats)
                endpoint_rows = audit_endpoint_rows(stats)
                summary = audit_stats_summary(stats, period_days=days)
                emit(make_envelope("audit stats summary", summary, {}), "json")
                emit(
                    make_envelope(
                        "audit stats by provider",
                        provider_rows,
                        {"rows": len(provider_rows)},
                    ),
                    "table",
                )
                emit(
                    make_envelope(
                        "audit stats by endpoint",
                        endpoint_rows,
                        {"rows": len(endpoint_rows)},
                    ),
                    "table",
                )
            else:
                print_json(data)
            log.close()
        except FinanceError as exc:
            error_exit("audit stats", exc)

    @audit_app.command("recent")
    def audit_recent(
        provider: str | None = typer.Option(
            None, "--provider", "-p", help="Filter by provider name."
        ),
        endpoint: str | None = typer.Option(
            None,
            "--endpoint",
            "-e",
            help="Filter by endpoint name (e.g. quote, price_history).",
        ),
        status: str | None = typer.Option(
            None,
            "--status",
            "-s",
            help=(
                "Filter by status: success, error, rate_limited, cache_hit, augment, "
                "skipped, not_supported, stale, all_failed."
            ),
        ),
        limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of entries to return."),
        config: str | None = typer.Option(
            os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
        ),
        fmt: str = typer.Option("json", "--format", "-f", help=_HELP_FORMAT),
    ) -> None:
        """Show recent audit log entries (newest first)."""
        try:
            log = AuditLog(retention_days=0)
            entries = log.query(
                provider=provider,
                endpoint=endpoint,
                status=status,
                limit=limit,
            )
            if fmt == "table":
                rows = [_recent_table_row(entry) for entry in entries]
            else:
                rows = [entry.to_dict() for entry in entries]
            emit(make_envelope("audit recent", rows, {"rows": len(rows)}), fmt)
            log.close()
        except FinanceError as exc:
            error_exit("audit recent", exc)

    @audit_app.command("path")
    def audit_path(
        config: str | None = typer.Option(
            os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
        ),
    ) -> None:
        """Print the audit log file path."""
        try:
            log = AuditLog(retention_days=0)
            path = log.path
            print_json(
                {
                    "path": str(path) if path else None,
                    "enabled": log.enabled,
                }
            )
            log.close()
        except FinanceError as exc:
            error_exit("audit path", exc)

    @audit_app.command("truncate")
    def audit_truncate(
        confirm: bool = typer.Option(
            False,
            "--confirm",
            help="Required: acknowledge that all audit entries will be permanently deleted.",
        ),
        config: str | None = typer.Option(
            os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
        ),
    ) -> None:
        """Truncate (clear) the audit log.

        All recorded entries are permanently deleted. Pass --confirm to proceed.

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
            log = AuditLog(retention_days=0)
            path = log.path
            log.clear()
            log.close()
            print_json({"status": "truncated", "path": str(path) if path else None})
        except FinanceError as exc:
            error_exit("audit truncate", exc)

    @audit_app.command("follow")
    def audit_follow(
        provider: str | None = typer.Option(
            None, "--provider", "-p", help="Filter by provider name."
        ),
        endpoint: str | None = typer.Option(
            None, "--endpoint", "-e", help="Filter by endpoint name."
        ),
        status: str | None = typer.Option(
            None, "--status", "-s", help="Filter by status (e.g. error, cache_hit)."
        ),
        symbol: str | None = typer.Option(
            None, "--symbol", help="Filter by ticker symbol (case-insensitive)."
        ),
        interval: float = typer.Option(0.5, "--interval", "-i", help="Poll interval in seconds."),
        config: str | None = typer.Option(
            os.environ.get("OFCLIENT_CONFIG"), "--config", help=_HELP_CONFIG
        ),
    ) -> None:
        """Stream new audit log entries as they arrive (like tail -f).

        Prints one JSON object per line to stdout. Press Ctrl-C to stop.

        Filters are applied to every new line, so only matching traffic is emitted.

        Examples:
          ofclient audit follow
          ofclient audit follow --status error
          ofclient audit follow --provider fmp --endpoint quote
          ofclient audit follow --symbol AAPL
        """
        try:
            log = AuditLog(retention_days=0)
            log_path = log.path
            enabled = log.enabled
            log.close()
            if log_path is None or not enabled:
                typer.echo("Audit log is disabled or path is not set.", err=True)
                raise typer.Exit(1)
            _follow_file(
                log_path,
                provider=provider,
                endpoint=endpoint,
                status=status,
                symbol=symbol,
                interval=interval,
            )
        except FinanceError as exc:
            error_exit("audit follow", exc)


def _recent_table_row(entry: Any) -> dict[str, Any]:
    timestamp = entry.timestamp.isoformat()
    time_part = timestamp.split("T")[1][:8] if "T" in timestamp else timestamp
    return {
        "time": time_part,
        "endpoint": entry.endpoint,
        "provider": entry.provider,
        "symbol": entry.symbol or "-",
        "status": entry.status,
        "ms": round(entry.latency_ms),
        "tier": f"{entry.tier_position + 1}/{entry.tier_total}",
        "fallback": "Y" if entry.is_fallback else "N",
        "error": entry.error_code or "",
    }


def _follow_file(
    log_path: Path,
    *,
    provider: str | None,
    endpoint: str | None,
    status: str | None,
    symbol: str | None,
    interval: float,
) -> None:
    """Tail an audit JSONL file, applying filters to newly appended rows."""
    try:
        byte_offset = log_path.stat().st_size if log_path.exists() else 0
    except OSError:
        byte_offset = 0
    typer.echo(f"Following {log_path}  (Ctrl-C to stop)", err=True)

    try:
        while True:
            if not log_path.exists():
                time.sleep(interval)
                continue
            current_size = log_path.stat().st_size
            if current_size < byte_offset:
                byte_offset = 0
            if current_size == byte_offset:
                time.sleep(interval)
                continue

            with open(log_path, "rb") as audit_file:
                audit_file.seek(byte_offset)
                new_bytes = audit_file.read()
                byte_offset = audit_file.tell()

            for line in new_bytes.decode("utf-8", errors="replace").splitlines():
                entry = _matching_entry(
                    line,
                    provider=provider,
                    endpoint=endpoint,
                    status=status,
                    symbol=symbol,
                )
                if entry is not None:
                    typer.echo(json.dumps(entry, separators=(",", ":")))
            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("\nStopped.", err=True)
        raise typer.Exit(0)


def _matching_entry(
    line: str,
    *,
    provider: str | None,
    endpoint: str | None,
    status: str | None,
    symbol: str | None,
) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(entry, dict):
        return None
    if provider and entry.get("provider") != provider:
        return None
    if endpoint and entry.get("endpoint") != endpoint:
        return None
    if status and entry.get("status") != status:
        return None
    if symbol and (entry.get("symbol") or "").upper() != symbol.upper():
        return None
    return entry
