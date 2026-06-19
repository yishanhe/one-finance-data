"""Data models for the audit log.

``AuditEntry`` is a single log row; ``AuditStats`` is aggregated
statistics over a time range.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class AuditEntry:
    """Single audit log entry for one provider call attempt.

    Attributes
    ----------
    timestamp:
        UTC time when the call started.
    request_id:
        Short hex ID grouping all tier-walk attempts for one
        ``dispatch()`` call.
    endpoint:
        Logical endpoint name (``"price_history"``, ``"quote"``, etc.).
    provider:
        Provider that handled this attempt (``"fmp"``, ``"cache"``, etc.).
    symbol:
        Ticker symbol, if extractable from the call context.
    status:
        Outcome — ``"success"``, ``"error"``, ``"rate_limited"``,
        ``"skipped"``, ``"cache_hit"``, ``"not_supported"``.
    latency_ms:
        Wall-clock time for this attempt in milliseconds.
    error_code:
        Stable error code (e.g. ``"NETWORK_ERROR"``), or ``None``.
    error_message:
        Human-readable error detail, or ``None``.
    tier_position:
        0-indexed position of this provider in the tier list.
    tier_total:
        Total number of providers in the tier list.
    http_status:
        Raw HTTP status code, if available.
    cache_key:
        Cache key for ``cache_hit`` entries, ``None`` otherwise.
    is_fallback:
        True when this attempt follows ≥1 real failure in the same request.
        Always False for the primary (first real) attempt.
    """

    timestamp: datetime
    request_id: str
    endpoint: str
    provider: str
    symbol: str | None = None
    status: str = "success"
    latency_ms: float = 0.0
    error_code: str | None = None
    error_message: str | None = None
    tier_position: int = 0
    tier_total: int = 1
    http_status: int | None = None
    cache_key: str | None = None
    is_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dictionary."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "request_id": self.request_id,
            "endpoint": self.endpoint,
            "provider": self.provider,
            "symbol": self.symbol,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 1),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "tier_position": self.tier_position,
            "tier_total": self.tier_total,
            "http_status": self.http_status,
            "cache_key": self.cache_key,
            "is_fallback": self.is_fallback,
        }


@dataclass
class AuditStats:
    """Aggregated statistics from audit entries.

    Attributes
    ----------
    total_calls:
        Total API call attempts (excludes cache hits, not_supported, skipped).
    cache_hits:
        Number of requests served from cache.
    cache_hit_rate:
        Fraction of total requests served from cache (0.0–1.0).
    calls_by_provider:
        Number of real API calls per provider.
    errors_by_provider:
        Number of errors per provider.
    avg_latency_ms_by_provider:
        Mean latency in ms per provider.
    rate_limits_by_provider:
        Number of rate-limit hits per provider.
    calls_by_endpoint:
        Number of real API calls per endpoint.
    errors_by_endpoint:
        Number of errors per endpoint.
    primary_failures_by_provider:
        Times each provider failed as the first real attempt (tier_position 0 in
        that request's real-attempt ordering), triggering a fallback.
    fallback_requests:
        Number of requests that needed ≥2 real provider attempts.
    fallback_rate:
        fallback_requests / requests_with_real_attempts (0.0–1.0).
    fallback_success_by_provider:
        Times each provider succeeded as a fallback (after a prior failure).
    fallback_failure_by_provider:
        Times each provider was tried as fallback but also failed.
    not_supported_by_provider:
        Times each provider returned ``not_supported`` (e.g. HTTP 402 plan limit).
    period_start:
        Start of the stats period.
    period_end:
        End of the stats period.
    """

    total_calls: int = 0
    cache_hits: int = 0
    cache_hit_rate: float = 0.0
    calls_by_provider: dict[str, int] = field(default_factory=dict)
    errors_by_provider: dict[str, int] = field(default_factory=dict)
    avg_latency_ms_by_provider: dict[str, float] = field(default_factory=dict)
    rate_limits_by_provider: dict[str, int] = field(default_factory=dict)
    calls_by_endpoint: dict[str, int] = field(default_factory=dict)
    errors_by_endpoint: dict[str, int] = field(default_factory=dict)
    primary_failures_by_provider: dict[str, int] = field(default_factory=dict)
    fallback_requests: int = 0
    fallback_rate: float = 0.0
    fallback_success_by_provider: dict[str, int] = field(default_factory=dict)
    fallback_failure_by_provider: dict[str, int] = field(default_factory=dict)
    not_supported_by_provider: dict[str, int] = field(default_factory=dict)
    period_start: datetime | None = None
    period_end: datetime | None = None
