"""Generate the machine-readable CLI manifest from the Typer registry."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import typer
from typer._click.core import Command, Parameter
from typer.core import TyperGroup

_FRESHNESS: dict[str, str] = {
    "price": "A",
    "financials": "A",
    "info": "A",
    "infos": "A",
    "insiders": "A",
    "indicators": "A",
    "news": "A",
    "actions": "A",
    "holders": "A",
    "analyst": "A",
    "peers": "A",
    "calendar": "A",
    "macro": "A",
    "treasury": "A",
    "earnings-date": "A",
    "estimates": "A",
    "quote": "B",
    "quotes": "B",
    "options": "B",
    "options-analytics": "B",
    "gex": "B",
    "maxpain": "B",
    "ivrank": "B",
    "short-interest": "B",
    "sentiment": "B",
    "screen": "B",
    "sector": "B",
    "ratios": "C",
    "earnings": "C",
}

_EXAMPLES: dict[str, list[str]] = {
    "price": ["ofclient price AAPL --range 1y"],
    "quote": ["ofclient quote AAPL"],
    "quotes": ["ofclient quotes AAPL MSFT"],
    "financials": ["ofclient financials AAPL --statement income --period annual"],
    "info": ["ofclient info AAPL"],
    "infos": ["ofclient infos AAPL MSFT"],
    "insiders": ["ofclient insiders AAPL --since 2024-01-01"],
    "ratios": ["ofclient ratios AAPL --period annual", "ofclient ratios AAPL --fresh"],
    "earnings": ["ofclient earnings AAPL"],
    "indicators": ["ofclient indicators AAPL --range 1y"],
    "options": ["ofclient options AAPL", "ofclient options AAPL --expiration 2026-06-20"],
    "providers check": ["ofclient providers check", "ofclient providers check --ping"],
}

_ARGUMENT_OVERLAYS: dict[tuple[str, str], dict[str, Any]] = {
    ("price", "--range"): {"type": "enum", "allowed": ["1m", "3m", "6m", "1y", "2y", "5y"]},
    (
        "indicators",
        "--range",
    ): {"type": "enum", "allowed": ["1m", "3m", "6m", "1y", "2y", "5y"]},
    (
        "financials",
        "--statement",
    ): {"type": "enum", "allowed": ["income", "balance", "cashflow"]},
    (
        "financials",
        "--period",
    ): {"type": "enum", "allowed": ["annual", "quarterly"]},
    ("ratios", "--period"): {"type": "enum", "allowed": ["annual", "quarterly"]},
}


def build_capabilities(app: typer.Typer) -> dict[str, Any]:
    """Build capabilities from registered commands and supplemental semantics."""
    root = typer.main.get_command(app)
    commands = [
        _command_capability(name, command)
        for name, command in _leaf_commands(root)
        if name != "capabilities"
    ]
    return {
        "schema_version": "1.0",
        "commands": sorted(commands, key=lambda command: str(command["name"])),
    }


def registered_command_names(app: typer.Typer) -> set[str]:
    """Return every public leaf command represented by the manifest."""
    root = typer.main.get_command(app)
    return {name for name, _command in _leaf_commands(root) if name != "capabilities"}


def _leaf_commands(
    command: Command,
    prefix: tuple[str, ...] = (),
) -> Iterator[tuple[str, Command]]:
    if isinstance(command, TyperGroup):
        for name, child in command.commands.items():
            yield from _leaf_commands(child, (*prefix, name))
        return
    yield " ".join(prefix), command


def _command_capability(name: str, command: Command) -> dict[str, Any]:
    description = " ".join((command.help or command.short_help or "").split())
    capability: dict[str, Any] = {
        "name": name,
        "description": description,
        "freshness_type": _FRESHNESS.get(name, "N/A"),
        "arguments": [_parameter_capability(name, parameter) for parameter in command.params],
        "examples": _EXAMPLES.get(name, [f"ofclient {name}"]),
    }
    if name == "indicators":
        capability["indicators"] = _indicator_fields()
    return capability


def _parameter_capability(command_name: str, parameter: Parameter) -> dict[str, Any]:
    name = _parameter_name(parameter)
    capability: dict[str, Any] = {
        "name": name,
        "required": bool(parameter.required),
        "type": _parameter_type(parameter),
    }
    default = parameter.default
    if default is not None and not callable(default):
        capability["default"] = default
    help_text = getattr(parameter, "help", None)
    if help_text:
        capability["description"] = help_text
    if hasattr(parameter.type, "choices"):
        capability["allowed"] = list(getattr(parameter.type, "choices"))
        capability["type"] = "enum"
    capability.update(_ARGUMENT_OVERLAYS.get((command_name, name), {}))
    return capability


def _parameter_name(parameter: Parameter) -> str:
    opts = getattr(parameter, "opts", ())
    long_options = [option for option in opts if option.startswith("--")]
    if long_options:
        return str(long_options[0])
    if opts:
        return str(opts[0])
    return str(parameter.human_readable_name)


def _parameter_type(parameter: Parameter) -> str:
    type_name = getattr(parameter.type, "name", None) or "string"
    mapped = {
        "text": "string",
        "integer": "integer",
        "float": "number",
        "boolean": "boolean",
        "datetime": "date",
    }.get(type_name, type_name)
    if parameter.multiple or parameter.nargs != 1:
        return f"list[{mapped}]"
    return mapped


def _indicator_fields() -> list[dict[str, Any]]:
    from onefinance.indicators.core import TechnicalIndicators

    schema = TechnicalIndicators.model_json_schema()
    required = set(schema.get("required", []))
    return [
        {
            "name": name,
            "type": _json_schema_type(field_schema),
            "required": name in required,
            **(
                {"description": field_schema["description"]}
                if field_schema.get("description")
                else {}
            ),
        }
        for name, field_schema in schema.get("properties", {}).items()
    ]


def _json_schema_type(schema: dict[str, Any]) -> str:
    if "anyOf" in schema:
        return "|".join(_json_schema_type(item) for item in schema["anyOf"])
    if "enum" in schema:
        return "enum"
    kind = str(schema.get("type", "object"))
    if kind == "array":
        return f"list[{_json_schema_type(schema.get('items', {}))}]"
    return {
        "number": "float",
        "integer": "integer",
        "string": "string",
        "boolean": "boolean",
        "null": "null",
    }.get(kind, kind)
