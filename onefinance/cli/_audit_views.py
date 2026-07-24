"""Pure CLI view adapters for audit statistics."""

from __future__ import annotations

from typing import Any

from onefinance.audit.models import AuditStats


def audit_stats_json(stats: AuditStats, *, period_days: int) -> dict[str, Any]:
    """Build the complete JSON representation for ``audit stats``."""
    return {
        **audit_stats_summary(stats, period_days=period_days),
        "cache_hits": stats.cache_hits,
        "avg_stale_age_s": stats.avg_stale_age_s,
        "failed_requests_by_endpoint": stats.failed_requests_by_endpoint,
        "augment_calls_by_provider": stats.augment_calls_by_provider,
        "augment_calls_by_endpoint": stats.augment_calls_by_endpoint,
        "augment_rate_by_endpoint": _percentages(stats.augment_rate_by_endpoint),
        "avg_augment_latency_ms_by_provider": stats.avg_augment_latency_ms_by_provider,
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
        "cache_hits_by_endpoint": stats.cache_hits_by_endpoint,
        "cache_hit_rate_by_endpoint": _percentages(stats.cache_hit_rate_by_endpoint),
    }


def audit_stats_summary(stats: AuditStats, *, period_days: int) -> dict[str, Any]:
    """Build the compact summary shared by JSON and table output."""
    return {
        "period_days": period_days,
        "total_api_calls": stats.total_calls,
        "cache_hit_rate": _percentage(stats.cache_hit_rate),
        "augment_calls": stats.augment_calls,
        "augment_rate": _percentage(stats.augment_rate),
        "stale_serves": stats.stale_serves,
        "stale_serve_rate": _percentage(stats.stale_serve_rate),
        "max_stale_age_s": stats.max_stale_age_s,
        "fallback_requests": stats.fallback_requests,
        "fallback_rate": _percentage(stats.fallback_rate),
        "failed_requests": stats.failed_requests,
    }


def audit_provider_rows(stats: AuditStats) -> list[dict[str, Any]]:
    """Build detailed per-provider rows for ``audit stats --format table``."""
    providers = _provider_names(stats)
    return [
        {
            "provider": provider,
            "calls": stats.calls_by_provider.get(provider, 0),
            "augment": stats.augment_calls_by_provider.get(provider, 0),
            "errors": stats.errors_by_provider.get(provider, 0),
            "not_supported": stats.not_supported_by_provider.get(provider, 0),
            "prim_fail": stats.primary_failures_by_provider.get(provider, 0),
            "fb_ok": stats.fallback_success_by_provider.get(provider, 0),
            "fb_fail": stats.fallback_failure_by_provider.get(provider, 0),
            "rate_limits": stats.rate_limits_by_provider.get(provider, 0),
            "latency_ms": stats.avg_latency_ms_by_provider.get(provider, 0),
            "p95_ms": stats.latency_p95_ms_by_provider.get(provider, 0),
        }
        for provider in providers
    ]


def cache_provider_rows(stats: AuditStats) -> list[dict[str, Any]]:
    """Build the compact provider view used by ``cache stats``."""
    return [
        {
            "provider": provider,
            "calls": stats.calls_by_provider.get(provider, 0),
            "errors": stats.errors_by_provider.get(provider, 0),
            "avg_latency_ms": stats.avg_latency_ms_by_provider.get(provider, 0),
        }
        for provider in _provider_names(stats)
    ]


def audit_endpoint_rows(stats: AuditStats) -> list[dict[str, Any]]:
    """Build per-endpoint rows for ``audit stats --format table``."""
    endpoints = sorted(
        set(stats.calls_by_endpoint)
        | set(stats.errors_by_endpoint)
        | set(stats.cache_hits_by_endpoint)
    )
    return [
        {
            "endpoint": endpoint,
            "calls": stats.calls_by_endpoint.get(endpoint, 0),
            "augment": stats.augment_calls_by_endpoint.get(endpoint, 0),
            "augment_rate": _percentage(stats.augment_rate_by_endpoint.get(endpoint, 0.0)),
            "errors": stats.errors_by_endpoint.get(endpoint, 0),
            "cache_hits": stats.cache_hits_by_endpoint.get(endpoint, 0),
            "hit_rate": _percentage(stats.cache_hit_rate_by_endpoint.get(endpoint, 0.0)),
        }
        for endpoint in endpoints
    ]


def _provider_names(stats: AuditStats) -> list[str]:
    return sorted(
        set(stats.calls_by_provider)
        | set(stats.errors_by_provider)
        | set(stats.not_supported_by_provider)
    )


def _percentage(value: float) -> str:
    return f"{value:.1%}"


def _percentages(values: dict[str, float]) -> dict[str, str]:
    return {name: _percentage(value) for name, value in sorted(values.items())}
