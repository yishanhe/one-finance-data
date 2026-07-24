"""Tests for reusable audit command helpers."""

from __future__ import annotations

import json

from onefinance.cli._audit_commands import _matching_entry


def test_matching_entry_accepts_matching_json_object() -> None:
    entry = {
        "provider": "fmp",
        "endpoint": "quote",
        "status": "success",
        "symbol": "AAPL",
    }

    assert (
        _matching_entry(
            json.dumps(entry),
            provider="fmp",
            endpoint="quote",
            status="success",
            symbol="aapl",
        )
        == entry
    )


def test_matching_entry_rejects_malformed_or_non_object_json() -> None:
    filters = {
        "provider": None,
        "endpoint": None,
        "status": None,
        "symbol": None,
    }

    assert _matching_entry("not-json", **filters) is None
    assert _matching_entry('["valid", "but", "not", "an", "entry"]', **filters) is None


def test_matching_entry_applies_each_filter() -> None:
    line = json.dumps(
        {
            "provider": "fmp",
            "endpoint": "quote",
            "status": "success",
            "symbol": "AAPL",
        }
    )
    base = {
        "provider": None,
        "endpoint": None,
        "status": None,
        "symbol": None,
    }

    for field, value in {
        "provider": "yfinance",
        "endpoint": "price_history",
        "status": "error",
        "symbol": "MSFT",
    }.items():
        filters = {**base, field: value}
        assert _matching_entry(line, **filters) is None
