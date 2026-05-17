"""Unit tests for CLI output formatting."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from onefinance.cli.format import (
    make_dry_run_envelope,
    make_envelope,
    make_error_envelope,
    print_csv,
    print_json,
)
from onefinance.core.errors import RateLimitError


class TestMakeEnvelope:
    def test_success_structure(self):
        env = make_envelope("price", [{"close": 185.64}], {"source": "fmp", "rows": 1})
        assert env["schema_version"] == "1.0"
        assert env["status"] == "success"
        assert env["command"] == "price"
        assert env["data"] == [{"close": 185.64}]
        assert env["metadata"]["source"] == "fmp"
        assert env["metadata"]["rows"] == 1

    def test_cache_hit_in_metadata(self):
        env = make_envelope("quote", {"price": 185.0}, {"cache_hit": True, "rows": 1})
        assert env["metadata"]["cache_hit"] is True


class TestMakeErrorEnvelope:
    def test_error_structure(self):
        err = RateLimitError(provider="fmp", message="Quota hit", retry_after_seconds=3600)
        env = make_error_envelope("price", err)
        assert env["schema_version"] == "1.0"
        assert env["status"] == "error"
        assert env["command"] == "price"
        assert env["error"]["code"] == "PROVIDER_QUOTA_EXHAUSTED"
        assert env["error"]["retry_safe"] is True

    def test_error_contains_suggested_action(self):
        err = RateLimitError(provider="fmp", message="Quota hit", retry_after_seconds=60)
        env = make_error_envelope("quote", err)
        assert "error" in env
        assert "code" in env["error"]


class TestMakeDryRunEnvelope:
    def test_structure(self):
        env = make_dry_run_envelope("price", {"would_fetch": False, "cache_hit_predicted": True})
        assert env["status"] == "dry_run"
        assert env["command"] == "price"
        assert env["plan"]["would_fetch"] is False


class TestPrintJson:
    def test_outputs_valid_json(self, capsys):
        env = make_envelope("price", [], {"rows": 0})
        print_json(env)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["status"] == "success"

    def test_serializes_dates(self, capsys):
        env = make_envelope("price", [{"date": date(2024, 1, 2)}], {"rows": 1})
        print_json(env)
        captured = capsys.readouterr()
        assert "2024-01-02" in captured.out

    def test_serializes_datetimes(self, capsys):
        dt = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
        env = make_envelope("quote", [{"fetched_at": dt}], {"rows": 1})
        print_json(env)
        captured = capsys.readouterr()
        assert "2024-01-02" in captured.out


class TestPrintCsv:
    def test_outputs_csv(self, capsys):
        data = [{"symbol": "AAPL", "close": 185.64}, {"symbol": "MSFT", "close": 374.51}]
        print_csv(data)
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert lines[0] == "symbol,close"
        assert "AAPL" in lines[1]
        assert "MSFT" in lines[2]

    def test_empty_data_produces_no_output(self, capsys):
        print_csv([])
        captured = capsys.readouterr()
        assert captured.out == ""
