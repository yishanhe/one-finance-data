"""Tests for onefinance.audit — JSONL file-backed audit log."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from onefinance.audit.log import AuditLog
from onefinance.audit.models import AuditEntry, AuditStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(
    provider: str = "fmp",
    endpoint: str = "quote",
    status: str = "success",
    latency_ms: float = 150.0,
    **kwargs,
) -> AuditEntry:
    """Build an AuditEntry with sensible defaults."""
    return AuditEntry(
        timestamp=kwargs.pop("timestamp", datetime.now(timezone.utc)),
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

    def test_disabled_log_is_noop(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl", enabled=False)
        log.record(_entry())
        assert log.query() == []
        assert log.stats().total_calls == 0

    def test_record_creates_file(self, tmp_path: Path):
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(log_path=log_path)
        log.record(_entry())
        assert log_path.exists()

    def test_record_appends_jsonl(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="fmp"))
        log.record(_entry(provider="finnhub"))

        lines = (tmp_path / "audit.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2

        obj1 = json.loads(lines[0])
        obj2 = json.loads(lines[1])
        assert obj1["provider"] == "fmp"
        assert obj2["provider"] == "finnhub"

    def test_each_line_is_valid_json(self, tmp_path: Path):
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

    def test_query_returns_newest_first(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
        log.record(_entry(request_id="old", timestamp=t1))
        log.record(_entry(request_id="new", timestamp=t2))

        entries = log.query()
        assert entries[0].request_id == "new"
        assert entries[1].request_id == "old"

    def test_query_filter_by_provider(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="fmp"))
        log.record(_entry(provider="finnhub"))
        log.record(_entry(provider="fmp"))

        entries = log.query(provider="fmp")
        assert len(entries) == 2
        assert all(e.provider == "fmp" for e in entries)

    def test_query_filter_by_endpoint(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(endpoint="quote"))
        log.record(_entry(endpoint="price_history"))
        log.record(_entry(endpoint="quote"))

        entries = log.query(endpoint="quote")
        assert len(entries) == 2

    def test_query_filter_by_status(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(status="success"))
        log.record(_entry(status="error", error_code="NETWORK_ERROR"))
        log.record(_entry(status="success"))

        entries = log.query(status="error")
        assert len(entries) == 1
        assert entries[0].error_code == "NETWORK_ERROR"

    def test_query_filter_by_since(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        old = datetime(2024, 1, 1, tzinfo=timezone.utc)
        new = datetime(2024, 6, 1, tzinfo=timezone.utc)
        log.record(_entry(timestamp=old))
        log.record(_entry(timestamp=new))

        entries = log.query(since=datetime(2024, 3, 1, tzinfo=timezone.utc))
        assert len(entries) == 1

    def test_query_limit(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        for i in range(20):
            log.record(_entry(request_id=f"req-{i}"))

        entries = log.query(limit=5)
        assert len(entries) == 5

    def test_query_by_request_id(self, tmp_path: Path):
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

    def test_stats_empty(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        stats = log.stats()
        assert stats.total_calls == 0
        assert stats.cache_hits == 0

    def test_stats_counts_calls_by_provider(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="fmp"))
        log.record(_entry(provider="fmp"))
        log.record(_entry(provider="finnhub"))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert stats.total_calls == 3
        assert stats.calls_by_provider["fmp"] == 2
        assert stats.calls_by_provider["finnhub"] == 1

    def test_stats_cache_hits(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="cache", status="cache_hit"))
        log.record(_entry(provider="cache", status="cache_hit"))
        log.record(_entry(provider="fmp", status="success"))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert stats.cache_hits == 2
        assert stats.total_calls == 1
        assert stats.cache_hit_rate == pytest.approx(0.667, abs=0.01)

    def test_stats_errors_and_rate_limits(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="fmp", status="success"))
        log.record(_entry(provider="fmp", status="error"))
        log.record(_entry(provider="fmp", status="rate_limited"))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert stats.errors_by_provider["fmp"] == 2  # error + rate_limited
        assert stats.rate_limits_by_provider["fmp"] == 1

    def test_stats_avg_latency(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry(provider="fmp", latency_ms=100))
        log.record(_entry(provider="fmp", latency_ms=200))

        stats = log.stats(since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert stats.avg_latency_ms_by_provider["fmp"] == 150.0

    def test_stats_respects_since(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        old = datetime(2023, 1, 1, tzinfo=timezone.utc)
        new = datetime(2024, 6, 1, tzinfo=timezone.utc)
        log.record(_entry(timestamp=old, provider="fmp"))
        log.record(_entry(timestamp=new, provider="finnhub"))

        stats = log.stats(since=datetime(2024, 1, 1, tzinfo=timezone.utc))
        assert stats.total_calls == 1
        assert "finnhub" in stats.calls_by_provider
        assert "fmp" not in stats.calls_by_provider


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

class TestAuditMaintenance:
    """Pruning and clearing."""

    def test_prune_removes_old_entries(self, tmp_path: Path):
        log = AuditLog(
            log_path=tmp_path / "audit.jsonl",
            retention_days=7,
        )
        old = datetime.now(timezone.utc) - timedelta(days=10)
        recent = datetime.now(timezone.utc)

        log.record(_entry(timestamp=old, request_id="old"))
        log.record(_entry(timestamp=recent, request_id="recent"))

        pruned = log._prune()
        assert pruned == 1

        entries = log.query()
        assert len(entries) == 1
        assert entries[0].request_id == "recent"

    def test_clear_empties_file(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path / "audit.jsonl")
        log.record(_entry())
        log.record(_entry())
        log.clear()

        assert log.query() == []

    def test_directory_as_path_creates_audit_jsonl(self, tmp_path: Path):
        log = AuditLog(log_path=tmp_path)
        log.record(_entry())
        assert (tmp_path / "audit.jsonl").exists()


# ---------------------------------------------------------------------------
# AuditEntry serialisation
# ---------------------------------------------------------------------------

class TestAuditEntry:
    """AuditEntry model."""

    def test_to_dict_roundtrip(self):
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
