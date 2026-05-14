"""CLI output formatting — JSON/table/CSV rendering and envelope construction."""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from typing import Any

from onefinance.core.errors import FinanceError


def make_envelope(command: str, data: Any, metadata: dict[str, Any]) -> dict:
    """Build a standard success response envelope."""
    return {
        "schema_version": "1.0",
        "status": "success",
        "command": command,
        "data": data,
        "metadata": metadata,
    }


def make_error_envelope(command: str, error: FinanceError) -> dict:
    """Build a standard error response envelope."""
    return {
        "schema_version": "1.0",
        "status": "error",
        "command": command,
        "error": error.to_dict(),
    }


def make_dry_run_envelope(command: str, plan: dict) -> dict:
    """Build a dry-run response envelope."""
    return {
        "status": "dry_run",
        "command": command,
        "plan": plan,
    }


def print_json(envelope: dict) -> None:
    """Pretty-print an envelope as JSON to stdout."""
    print(json.dumps(envelope, indent=2, default=_json_default))


def print_table(data: list[dict], command: str) -> None:
    """Print data as a Rich table; falls back to JSON if Rich is unavailable."""
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        print_json({"data": data})
        return

    if not data:
        Console().print("[yellow]No data[/yellow]")
        return

    console = Console()
    table = Table(show_header=True, header_style="bold cyan")
    columns = list(data[0].keys())
    for col in columns:
        table.add_column(col)
    for row in data:
        table.add_row(*[str(row.get(col, "")) for col in columns])
    console.print(table)


def print_csv(data: list[dict]) -> None:
    """Print data as CSV to stdout."""
    if not data:
        return
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(data[0].keys()), lineterminator="\n")
    writer.writeheader()
    writer.writerows(data)
    print(output.getvalue(), end="")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
