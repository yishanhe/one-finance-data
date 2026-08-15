"""Tests for pure audit CLI view adapters."""

from onefinance.audit.models import AuditStats
from onefinance.cli._audit_views import (
    audit_endpoint_rows,
    audit_provider_rows,
    audit_stats_json,
    cache_provider_rows,
)


def _stats() -> AuditStats:
    return AuditStats(
        total_calls=5,
        cache_hits=2,
        cache_hit_rate=0.286,
        augment_calls=1,
        augment_cache_hits=3,
        augment_cache_hit_rate=0.75,
        augment_rate=0.2,
        stale_serves=1,
        stale_serve_rate=0.125,
        calls_by_provider={"yfinance": 3, "finnhub": 2},
        errors_by_provider={"finnhub": 1},
        not_supported_by_provider={"cboe": 1},
        augment_calls_by_provider={"yfinance": 1},
        avg_latency_ms_by_provider={"yfinance": 400.0, "finnhub": 180.0},
        latency_p95_ms_by_provider={"yfinance": 500.0, "finnhub": 200.0},
        calls_by_endpoint={"quote": 5},
        errors_by_endpoint={"quote": 1},
        cache_hits_by_endpoint={"quote": 2},
        cache_hit_rate_by_endpoint={"quote": 0.286},
        augment_calls_by_endpoint={"quote": 1},
        augment_rate_by_endpoint={"quote": 0.2},
    )


def test_json_view_formats_rates_and_preserves_breakdowns() -> None:
    view = audit_stats_json(_stats(), period_days=7)

    assert view["period_days"] == 7
    assert view["cache_hit_rate"] == "28.6%"
    assert view["stale_serve_rate"] == "12.5%"
    assert view["augment_rate_by_endpoint"] == {"quote": "20.0%"}
    assert view["augment_cache_hits"] == 3
    assert view["augment_cache_hit_rate"] == "75.0%"
    assert view["calls_by_provider"] == {"yfinance": 3, "finnhub": 2}


def test_provider_views_share_complete_sorted_provider_set() -> None:
    stats = _stats()

    detailed = audit_provider_rows(stats)
    compact = cache_provider_rows(stats)

    assert [row["provider"] for row in detailed] == ["cboe", "finnhub", "yfinance"]
    assert [row["provider"] for row in compact] == ["cboe", "finnhub", "yfinance"]
    assert detailed[2]["augment"] == 1


def test_endpoint_view_formats_request_level_rates() -> None:
    rows = audit_endpoint_rows(_stats())

    assert rows == [
        {
            "endpoint": "quote",
            "calls": 5,
            "augment": 1,
            "augment_rate": "20.0%",
            "errors": 1,
            "cache_hits": 2,
            "hit_rate": "28.6%",
        }
    ]
