"""Tests for onefinance.audit — JSONL file-backed audit log."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from onefinance.audit._recorder import AuditRecorder
from onefinance.audit.log import AuditLog, _parse_ts
from onefinance.audit.models import AuditEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    provider: str = "fmp",
    endpoint: str = "quote",
    status: str = "success",
    latency_ms: float = 150.0,
    **kwargs: Any,
) -> AuditEntry:
    """Build an AuditEntry with sensible defaults."""
    return AuditEntry(
        timestamp=kwargs.pop("timestamp", datetime.now(UTC)),
        request_id=kwargs.pop("request_id", "abc123"),
        endpoint=endpoint,
        provider=provider,
        status=status,
        latency_ms=latency_ms,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# AuditLog basics
# ---------------------------------------------------------------------------


class TestAuditLogBasics:
    """Core record/query/stats operations."""

    def test_disabled_log_is_noop(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl", enabled=False)
        log.record(_entry())
        assert log.query() == []
        assert log.stats().total_calls == 0

    def test_record_creates_file(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(log_path=log_path)
        log.record(_entry())
        assert log_path.exists()

    def test_record_appends_jsonl(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="fmp"))
        log.record(_entry(provider="finnhub"))

        lines = (tmp_path / "audit.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2

        obj1 = json.loads(lines[0])
        obj2 = json.loads(lines[1])
        assert obj1["provider"] == "fmp"
        assert obj2["provider"] == "finnhub"

    def test_each_line_is_valid_json(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        for i in range(5):
            log.record(_entry(request_id=f"req-{i}"))

        lines = (tmp_path / "audit.jsonl").read_text().strip().split("\n")
        for line in lines:
            obj = json.loads(line)
            assert "request_id" in obj
            assert "provider" in obj


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


class TestAuditQuery:
    """Filtering and ordering in query()."""

    def test_query_returns_newest_first(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        t1 = datetime(2024, 1, 1, tzinfo=UTC)
        t2 = datetime(2024, 1, 2, tzinfo=UTC)
        log.record(_entry(request_id="old", timestamp=t1))
        log.record(_entry(request_id="new", timestamp=t2))

        entries = log.query()
        assert entries[0].request_id == "new"
        assert entries[1].request_id == "old"

    def test_query_filter_by_provider(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="fmp"))
        log.record(_entry(provider="finnhub"))
        log.record(_entry(provider="fmp"))

        entries = log.query(provider="fmp")
        assert len(entries) == 2
        assert all(e.provider == "fmp" for e in entries)

    def test_query_filter_by_endpoint(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(endpoint="quote"))
        log.record(_entry(endpoint="price_history"))
        log.record(_entry(endpoint="quote"))

        entries = log.query(endpoint="quote")
        assert len(entries) == 2

    def test_query_filter_by_status(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(status="success"))
        log.record(_entry(status="error", error_code="NETWORK_ERROR"))
        log.record(_entry(status="success"))

        entries = log.query(status="error")
        assert len(entries) == 1
        assert entries[0].error_code == "NETWORK_ERROR"

    def test_query_filter_by_since(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        old = datetime(2024, 1, 1, tzinfo=UTC)
        new = datetime(2024, 6, 1, tzinfo=UTC)
        log.record(_entry(timestamp=old))
        log.record(_entry(timestamp=new))

        entries = log.query(since=datetime(2024, 3, 1, tzinfo=UTC))
        assert len(entries) == 1

    def test_query_limit(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        for i in range(20):
            log.record(_entry(request_id=f"req-{i}"))

        entries = log.query(limit=5)
        assert len(entries) == 5

    def test_query_by_request_id(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(request_id="aaa", provider="fmp", status="error"))
        log.record(_entry(request_id="aaa", provider="finnhub", status="success"))
        log.record(_entry(request_id="bbb", provider="fmp", status="success"))

        entries = log.query(request_id="aaa")
        assert len(entries) == 2
        # Both should share the same request_id
        providers = {e.provider for e in entries}
        assert providers == {"fmp", "finnhub"}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestAuditStats:
    """Aggregated statistics computation."""

    def test_stats_empty(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        stats = log.stats()
        assert stats.total_calls == 0
        assert stats.cache_hits == 0

    def test_stats_counts_calls_by_provider(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="fmp"))
        log.record(_entry(provider="fmp"))
        log.record(_entry(provider="finnhub"))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.total_calls == 3
        assert stats.calls_by_provider["fmp"] == 2
        assert stats.calls_by_provider["finnhub"] == 1

    def test_stats_cache_hits(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="cache", status="cache_hit"))
        log.record(_entry(provider="cache", status="cache_hit"))
        log.record(_entry(provider="fmp", status="success"))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.cache_hits == 2
        assert stats.total_calls == 1
        assert stats.cache_hit_rate == pytest.approx(0.667, abs=0.01)

    def test_stats_stale_serves_not_counted_as_calls(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="cache", status="stale"))
        log.record(_entry(provider="cache", status="stale"))
        log.record(_entry(provider="fmp", status="success"))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.stale_serves == 2
        # A stale serve made no provider call — must not inflate call counts.
        assert stats.total_calls == 1
        assert "cache" not in stats.calls_by_provider

    def test_stats_stale_serve_rate_and_age(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="cache", status="stale", stale_age_s=100.0))
        log.record(_entry(provider="cache", status="stale", stale_age_s=300.0))
        log.record(_entry(provider="cache", status="cache_hit"))
        log.record(_entry(provider="fmp", status="success"))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        # Denominator = calls + cache_hits + stale_serves = 1 + 1 + 2 = 4.
        assert stats.stale_serves == 2
        assert stats.stale_serve_rate == pytest.approx(0.5, abs=0.01)
        assert stats.cache_hit_rate == pytest.approx(0.25, abs=0.01)
        assert stats.avg_stale_age_s == pytest.approx(200.0, abs=0.01)
        assert stats.max_stale_age_s == pytest.approx(300.0, abs=0.01)

    def test_stats_errors_and_rate_limits(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="fmp", status="success"))
        log.record(_entry(provider="fmp", status="error"))
        log.record(_entry(provider="fmp", status="rate_limited"))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.errors_by_provider["fmp"] == 2  # error + rate_limited
        assert stats.rate_limits_by_provider["fmp"] == 1

    def test_stats_avg_latency(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="fmp", latency_ms=100))
        log.record(_entry(provider="fmp", latency_ms=200))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.avg_latency_ms_by_provider["fmp"] == 150.0

    def test_stats_latency_percentiles(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        for ms in (100, 200, 300, 400, 500):
            log.record(_entry(provider="fmp", latency_ms=ms))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        # Nearest-rank over [100,200,300,400,500]:
        #   p50 → idx ceil(.5*5)-1 = 2 → 300; p95/p99 → idx 4 → 500.
        assert stats.latency_p50_ms_by_provider["fmp"] == 300.0
        assert stats.latency_p95_ms_by_provider["fmp"] == 500.0
        assert stats.latency_p99_ms_by_provider["fmp"] == 500.0

    def test_stats_latency_percentiles_single_sample(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="fmp", latency_ms=250))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        # n=1 → every percentile is the single value.
        assert stats.latency_p50_ms_by_provider["fmp"] == 250.0
        assert stats.latency_p99_ms_by_provider["fmp"] == 250.0

    def test_stats_respects_since(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        old = datetime(2023, 1, 1, tzinfo=UTC)
        new = datetime(2024, 6, 1, tzinfo=UTC)
        log.record(_entry(timestamp=old, provider="fmp"))
        log.record(_entry(timestamp=new, provider="finnhub"))

        stats = log.stats(since=datetime(2024, 1, 1, tzinfo=UTC))
        assert stats.total_calls == 1
        assert "finnhub" in stats.calls_by_provider
        assert "fmp" not in stats.calls_by_provider

    def test_stats_calls_by_endpoint(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(endpoint="quote", status="success"))
        log.record(_entry(endpoint="quote", status="success"))
        log.record(_entry(endpoint="financials", status="success"))
        log.record(_entry(endpoint="financials", status="error"))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.calls_by_endpoint["quote"] == 2
        assert stats.calls_by_endpoint["financials"] == 2
        assert stats.errors_by_endpoint["financials"] == 1
        assert "quote" not in stats.errors_by_endpoint

    def test_stats_endpoint_totals_match_total_calls(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(endpoint="quote", status="success"))
        log.record(_entry(endpoint="financials", status="error"))
        log.record(_entry(endpoint="quote", status="cache_hit"))
        log.record(_entry(endpoint="quote", status="not_supported"))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        endpoint_total = sum(stats.calls_by_endpoint.values())
        assert endpoint_total == stats.total_calls

    def test_fallback_detected_when_two_real_attempts(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        # Request "req1": fmp fails as primary, yfinance succeeds as fallback
        log.record(_entry(request_id="req1", provider="fmp", status="error", tier_position=0))
        log.record(
            _entry(request_id="req1", provider="yfinance", status="success", tier_position=1)
        )

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.fallback_requests == 1
        assert stats.fallback_rate == pytest.approx(1.0)
        assert stats.primary_failures_by_provider["fmp"] == 1

    def test_fallback_not_triggered_by_not_supported(self, tmp_path: Path) -> None:
        """not_supported at low tier positions must not count as a real attempt."""
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        # fmp and finnhub don't support this endpoint; yfinance succeeds
        log.record(
            _entry(request_id="req1", provider="fmp", status="not_supported", tier_position=0)
        )
        log.record(
            _entry(request_id="req1", provider="finnhub", status="not_supported", tier_position=1)
        )
        log.record(
            _entry(request_id="req1", provider="yfinance", status="success", tier_position=2)
        )

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.fallback_requests == 0
        assert stats.fallback_rate == 0.0
        assert stats.primary_failures_by_provider == {}

    def test_fallback_not_triggered_by_skipped(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(request_id="req1", provider="fmp", status="skipped", tier_position=0))
        log.record(
            _entry(request_id="req1", provider="yfinance", status="success", tier_position=1)
        )

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.fallback_requests == 0
        assert stats.primary_failures_by_provider == {}

    def test_fallback_not_triggered_by_augment(self, tmp_path: Path) -> None:
        """An augment entry on the same request_id must not count as a fallback.

        A primary success + augment call for the same request is enrichment,
        not a failure-driven fallback, so fallback_requests must stay 0.
        """
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(request_id="req1", provider="finnhub", status="success", tier_position=0))
        log.record(
            _entry(request_id="req1", provider="yfinance", status="augment", tier_position=1)
        )

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.fallback_requests == 0
        assert stats.fallback_rate == 0.0
        assert stats.primary_failures_by_provider == {}
        # Augment call IS a real API call — counted in total_calls
        assert stats.total_calls == 2

    def test_fallback_rate_mixed_requests(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        # req1: single success — no fallback
        log.record(_entry(request_id="req1", provider="fmp", status="success", tier_position=0))
        # req2: fmp fails, yfinance succeeds — fallback
        log.record(_entry(request_id="req2", provider="fmp", status="error", tier_position=0))
        log.record(
            _entry(request_id="req2", provider="yfinance", status="success", tier_position=1)
        )

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.fallback_requests == 1
        assert stats.fallback_rate == pytest.approx(0.5)
        assert stats.primary_failures_by_provider["fmp"] == 1

    def test_fallback_success_by_provider_tracked(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(request_id="req1", provider="fmp", status="error", tier_position=0))
        log.record(
            _entry(request_id="req1", provider="yfinance", status="success", tier_position=1)
        )

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.fallback_success_by_provider["yfinance"] == 1
        assert "fmp" not in stats.fallback_success_by_provider

    def test_fallback_failure_by_provider_tracked(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        # Both providers fail: fmp primary failure, finnhub fallback failure
        log.record(_entry(request_id="req1", provider="fmp", status="error", tier_position=0))
        log.record(_entry(request_id="req1", provider="finnhub", status="error", tier_position=1))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.fallback_failure_by_provider["finnhub"] == 1
        assert "fmp" not in stats.fallback_failure_by_provider

    def test_fallback_fields_empty_when_no_fallbacks(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(request_id="req1", provider="fmp", status="success", tier_position=0))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.fallback_success_by_provider == {}
        assert stats.fallback_failure_by_provider == {}

    def test_is_fallback_field_in_entry(self) -> None:
        entry = _entry(is_fallback=True)
        assert entry.is_fallback is True
        d = entry.to_dict()
        assert d["is_fallback"] is True

    def test_is_fallback_defaults_false(self) -> None:
        entry = _entry()
        assert entry.is_fallback is False
        assert entry.to_dict()["is_fallback"] is False

    def test_is_fallback_round_trips_through_jsonl(self, tmp_path: Path) -> None:
        import json

        from onefinance.audit.log import _dict_to_entry

        entry = _entry(is_fallback=True)
        line = json.dumps(entry.to_dict())
        restored = _dict_to_entry(json.loads(line))
        assert restored.is_fallback is True

    def test_old_entry_without_is_fallback_defaults_false(self, tmp_path: Path) -> None:
        from onefinance.audit.log import _dict_to_entry

        old_style = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "request_id": "abc",
            "endpoint": "quote",
            "provider": "fmp",
            "status": "success",
            "latency_ms": 100.0,
        }
        entry = _dict_to_entry(old_style)
        assert entry.is_fallback is False

    def test_multi_level_fallback_chain(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        # fmp fails → finnhub fails → yfinance succeeds
        log.record(_entry(request_id="req1", provider="fmp", status="error", tier_position=0))
        log.record(_entry(request_id="req1", provider="finnhub", status="error", tier_position=1))
        log.record(
            _entry(request_id="req1", provider="yfinance", status="success", tier_position=2)
        )

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert stats.fallback_requests == 1
        assert stats.primary_failures_by_provider["fmp"] == 1
        assert stats.fallback_failure_by_provider["finnhub"] == 1
        assert stats.fallback_success_by_provider["yfinance"] == 1


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


class TestAuditMaintenance:
    """Pruning and clearing."""

    def test_prune_removes_old_entries(self, tmp_path: Path) -> None:
        log = AuditLog(
            log_path=tmp_path / "audit.jsonl",
            retention_days=7,
        )
        old = datetime.now(UTC) - timedelta(days=10)
        recent = datetime.now(UTC)

        log.record(_entry(timestamp=old, request_id="old"))
        log.record(_entry(timestamp=recent, request_id="recent"))

        pruned = log._prune()
        assert pruned == 1

        entries = log.query()
        assert len(entries) == 1
        assert entries[0].request_id == "recent"

    def test_clear_empties_file(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry())
        log.record(_entry())
        log.clear()

        assert log.query() == []

    def test_directory_as_path_creates_audit_jsonl(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path)
        log.record(_entry())
        assert (tmp_path / "audit.jsonl").exists()


# ---------------------------------------------------------------------------
# AuditEntry serialisation
# ---------------------------------------------------------------------------


class TestAuditEntry:
    """AuditEntry model."""

    def test_to_dict_roundtrip(self) -> None:
        entry = _entry(
            provider="fmp",
            endpoint="quote",
            symbol="AAPL",
            status="success",
            latency_ms=123.456,
        )
        d = entry.to_dict()
        assert d["provider"] == "fmp"
        assert d["symbol"] == "AAPL"
        assert d["latency_ms"] == 123.5  # rounded to 1 decimal
        assert isinstance(d["timestamp"], str)


# ---------------------------------------------------------------------------
# Edge cases — covering remaining missing lines
# ---------------------------------------------------------------------------


class TestAuditLogEdgeCases:
    def test_query_skips_invalid_json_lines(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry())
        (tmp_path / "audit.jsonl").open("a").write("NOT JSON\n")
        results = log.query()
        assert len(results) == 1

    def test_record_swallows_write_error(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        with patch("builtins.open", side_effect=OSError("disk full")):
            log.record(_entry())  # should not raise

    def test_query_filter_by_symbol(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(symbol="AAPL"))
        log.record(_entry(symbol="MSFT"))
        results = log.query(symbol="aapl")
        assert len(results) == 1

    def test_stats_without_since_uses_default(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry())
        stats = log.stats()  # since=None → defaults to 24h ago
        assert stats.total_calls >= 1

    def test_stats_skips_bad_json(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry())
        (tmp_path / "audit.jsonl").open("a").write("BAD JSON\n")
        stats = log.stats()
        assert stats.total_calls >= 1

    def test_stats_skips_not_supported_status(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(status="success"))
        log.record(_entry(status="not_supported"))
        log.record(_entry(status="skipped"))
        stats = log.stats()
        assert stats.total_calls == 1

    def test_prune_skips_bad_json(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text('{"timestamp": "bad"}\nNOT JSON\n')
        log = AuditLog(log_path=path, retention_days=1)
        # Just checking it doesn't raise
        assert log is not None

    def test_path_property(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        assert log.path is not None

    def test_read_lines_empty_when_no_file(self, tmp_path: Path) -> None:
        log = AuditLog(log_path=tmp_path / "nonexistent.jsonl")
        assert log.query() == []

    def test_prune_during_init_removes_old(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
        path.write_text(json.dumps({"timestamp": old_ts, "request_id": "x"}) + "\n")
        log = AuditLog(log_path=path, retention_days=30)
        assert log.query() == []


class TestParseTs:
    def test_empty_string_returns_none(self) -> None:
        assert _parse_ts("") is None

    def test_invalid_string_returns_none(self) -> None:
        assert _parse_ts("not-a-date") is None

    def test_valid_iso_returns_datetime(self) -> None:
        ts = _parse_ts("2024-01-02T12:00:00+00:00")
        assert ts is not None


class TestAuditRecorder:
    def test_record_success_when_enabled(self, tmp_path: Path) -> None:
        audit_log = AuditLog(log_path=tmp_path / "audit.jsonl")
        recorder = AuditRecorder(audit_log)
        recorder.record_success(
            request_id="req1",
            endpoint="quote",
            provider="fmp",
            latency_ms=100.0,
            tier_position=1,
            tier_total=2,
            symbol="AAPL",
        )
        entries = audit_log.query()
        assert len(entries) == 1
        assert entries[0].symbol == "AAPL"

    def test_record_cache_hit_with_symbol(self, tmp_path: Path) -> None:
        audit_log = AuditLog(log_path=tmp_path / "audit.jsonl")
        recorder = AuditRecorder(audit_log)
        recorder.record_cache_hit(
            request_id="req1",
            endpoint="price_history",
            cache_key="price_history:abc123",
            symbol="MSFT",
        )
        entries = audit_log.query()
        assert len(entries) == 1
        assert entries[0].symbol == "MSFT"
        assert entries[0].provider == "cache"

    def test_record_failure_with_symbol(self, tmp_path: Path) -> None:
        audit_log = AuditLog(log_path=tmp_path / "audit.jsonl")
        recorder = AuditRecorder(audit_log)
        recorder.record_failure(
            request_id="req1",
            endpoint="quote",
            provider="fmp",
            latency_ms=519.1,
            tier_position=0,
            tier_total=2,
            error_code="NETWORK_ERROR",
            error_message="HTTP 402",
            rate_limited=False,
            symbol="TSLA",
        )
        entries = audit_log.query()
        assert len(entries) == 1
        assert entries[0].symbol == "TSLA"
        assert entries[0].status == "error"

    def test_symbol_none_when_not_provided(self, tmp_path: Path) -> None:
        """Backwards compat: symbol defaults to None if not passed."""
        audit_log = AuditLog(log_path=tmp_path / "audit.jsonl")
        recorder = AuditRecorder(audit_log)
        recorder.record_success(
            request_id="req1",
            endpoint="quote",
            provider="fmp",
            latency_ms=100.0,
            tier_position=1,
            tier_total=2,
        )
        entries = audit_log.query()
        assert len(entries) == 1
        assert entries[0].symbol is None

    def test_record_swallows_exception(self, tmp_path: Path) -> None:
        audit_log = AuditLog(log_path=tmp_path / "audit.jsonl")
        recorder = AuditRecorder(audit_log)
        with patch.object(audit_log, "record", side_effect=RuntimeError("fail")):
            recorder.record_success(
                request_id="req1",
                endpoint="quote",
                provider="fmp",
                latency_ms=100.0,
                tier_position=1,
                tier_total=2,
            )  # should not raise
