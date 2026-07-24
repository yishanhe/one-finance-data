"""Pure aggregation for decoded audit rows.

This module deliberately knows nothing about JSONL files. ``AuditLog`` owns
storage and parsing; ``AuditStatsAccumulator`` owns event classification and
request-level metric calculation.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

from onefinance.audit.models import AuditStats

_ATTEMPT_STATUSES = frozenset({"success", "error", "rate_limited"})


class AuditStatsAccumulator:
    """Accumulate decoded audit rows and build one stats snapshot."""

    def __init__(self) -> None:
        self.total_calls = 0
        self.cache_hits = 0
        self.stale_serves = 0
        self.failed_requests = 0
        self.failed_by_endpoint: dict[str, int] = defaultdict(int)
        self.stale_ages: list[float] = []
        self.calls_by_provider: dict[str, int] = defaultdict(int)
        self.errors_by_provider: dict[str, int] = defaultdict(int)
        self.rate_limits_by_provider: dict[str, int] = defaultdict(int)
        self.latencies_by_provider: dict[str, list[float]] = defaultdict(list)
        self.calls_by_endpoint: dict[str, int] = defaultdict(int)
        self.errors_by_endpoint: dict[str, int] = defaultdict(int)
        self.not_supported_by_provider: dict[str, int] = defaultdict(int)
        self.augment_calls = 0
        self.augment_by_provider: dict[str, int] = defaultdict(int)
        self.augment_by_endpoint: dict[str, int] = defaultdict(int)
        self.augment_latencies_by_provider: dict[str, list[float]] = defaultdict(list)
        self.augmented_request_ids: set[str] = set()
        self.augmented_request_ids_by_endpoint: dict[str, set[str]] = defaultdict(set)
        self.cache_hits_by_endpoint: dict[str, int] = defaultdict(int)
        self.stale_by_endpoint: dict[str, int] = defaultdict(int)
        self.attempts_by_request: dict[str, list[dict[str, object]]] = defaultdict(list)

    def add(self, row: dict[str, object]) -> None:
        """Classify and accumulate one decoded audit row."""
        status = str(row.get("status", ""))
        endpoint = str(row.get("endpoint", "unknown"))
        provider = str(row.get("provider", "unknown"))

        if status == "cache_hit":
            self.cache_hits += 1
            self.cache_hits_by_endpoint[endpoint] += 1
            return
        if status == "stale":
            self.stale_serves += 1
            self.stale_by_endpoint[endpoint] += 1
            age = row.get("stale_age_s")
            if age is not None:
                self.stale_ages.append(_as_float(age))
            return
        if status == "skipped":
            return
        if status == "not_supported":
            self.not_supported_by_provider[provider] += 1
            return
        if status == "all_failed":
            self.failed_requests += 1
            self.failed_by_endpoint[endpoint] += 1
            return

        self.total_calls += 1
        self.calls_by_provider[provider] += 1
        self.calls_by_endpoint[endpoint] += 1
        latency_ms = _as_float(row.get("latency_ms", 0))
        self.latencies_by_provider[provider].append(latency_ms)

        if status == "augment":
            self.augment_calls += 1
            self.augment_by_provider[provider] += 1
            self.augment_by_endpoint[endpoint] += 1
            self.augment_latencies_by_provider[provider].append(latency_ms)
            request_id = str(row.get("request_id", ""))
            if request_id:
                self.augmented_request_ids.add(request_id)
                self.augmented_request_ids_by_endpoint[endpoint].add(request_id)

        if status == "error":
            self.errors_by_provider[provider] += 1
            self.errors_by_endpoint[endpoint] += 1
        elif status == "rate_limited":
            self.rate_limits_by_provider[provider] += 1
            self.errors_by_provider[provider] += 1
            self.errors_by_endpoint[endpoint] += 1

        request_id = str(row.get("request_id", ""))
        if request_id and status in _ATTEMPT_STATUSES:
            self.attempts_by_request[request_id].append(row)

    def build(self, *, period_start: datetime, period_end: datetime) -> AuditStats:
        """Finish request-level calculations and return the public snapshot."""
        (
            fallback_requests,
            primary_failures,
            fallback_successes,
            fallback_failures,
        ) = self._fallback_metrics()

        provider_requests = len(self.attempts_by_request)
        total_requests = provider_requests + self.cache_hits + self.stale_serves
        requests_by_endpoint = self._requests_by_endpoint()

        cache_hit_rates = self._cache_hit_rates(requests_by_endpoint)
        augment_rates = self._augment_rates(requests_by_endpoint)
        latency_p50, latency_p95, latency_p99 = _latency_percentiles(self.latencies_by_provider)

        return AuditStats(
            total_calls=self.total_calls,
            cache_hits=self.cache_hits,
            cache_hit_rate=_ratio(self.cache_hits, total_requests),
            stale_serves=self.stale_serves,
            stale_serve_rate=_ratio(self.stale_serves, total_requests),
            avg_stale_age_s=round(_mean(self.stale_ages), 1),
            max_stale_age_s=round(max(self.stale_ages, default=0.0), 1),
            calls_by_provider=dict(self.calls_by_provider),
            errors_by_provider=dict(self.errors_by_provider),
            avg_latency_ms_by_provider=_means(self.latencies_by_provider),
            latency_p50_ms_by_provider=latency_p50,
            latency_p95_ms_by_provider=latency_p95,
            latency_p99_ms_by_provider=latency_p99,
            rate_limits_by_provider=dict(self.rate_limits_by_provider),
            calls_by_endpoint=dict(self.calls_by_endpoint),
            errors_by_endpoint=dict(self.errors_by_endpoint),
            primary_failures_by_provider=dict(primary_failures),
            fallback_requests=fallback_requests,
            fallback_rate=_ratio(fallback_requests, provider_requests),
            fallback_success_by_provider=dict(fallback_successes),
            fallback_failure_by_provider=dict(fallback_failures),
            augment_calls=self.augment_calls,
            augment_rate=_ratio(
                len(self.augmented_request_ids & self.attempts_by_request.keys()),
                provider_requests,
            ),
            augment_calls_by_provider=dict(self.augment_by_provider),
            augment_calls_by_endpoint=dict(self.augment_by_endpoint),
            augment_rate_by_endpoint=augment_rates,
            avg_augment_latency_ms_by_provider=_means(self.augment_latencies_by_provider),
            cache_hits_by_endpoint=dict(self.cache_hits_by_endpoint),
            cache_hit_rate_by_endpoint=cache_hit_rates,
            not_supported_by_provider=dict(self.not_supported_by_provider),
            failed_requests=self.failed_requests,
            failed_requests_by_endpoint=dict(self.failed_by_endpoint),
            period_start=period_start,
            period_end=period_end,
        )

    def _fallback_metrics(
        self,
    ) -> tuple[
        int,
        dict[str, int],
        dict[str, int],
        dict[str, int],
    ]:
        primary_failures: dict[str, int] = defaultdict(int)
        fallback_successes: dict[str, int] = defaultdict(int)
        fallback_failures: dict[str, int] = defaultdict(int)
        fallback_requests = 0

        for attempts in self.attempts_by_request.values():
            attempts.sort(key=lambda row: _as_int(row.get("tier_position") or 0))
            if len(attempts) >= 2:
                fallback_requests += 1
            first = attempts[0]
            if first.get("status") in ("error", "rate_limited"):
                primary_failures[str(first.get("provider", "unknown"))] += 1
            for attempt in attempts[1:]:
                provider = str(attempt.get("provider", "unknown"))
                if attempt.get("status") == "success":
                    fallback_successes[provider] += 1
                elif attempt.get("status") in ("error", "rate_limited"):
                    fallback_failures[provider] += 1

        return (
            fallback_requests,
            primary_failures,
            fallback_successes,
            fallback_failures,
        )

    def _requests_by_endpoint(self) -> dict[str, int]:
        requests: dict[str, int] = defaultdict(int)
        for attempts in self.attempts_by_request.values():
            requests[str(attempts[0].get("endpoint", "unknown"))] += 1
        return requests

    def _augment_rates(self, requests_by_endpoint: dict[str, int]) -> dict[str, float]:
        return {
            endpoint: _ratio(
                len(request_ids & self.attempts_by_request.keys()),
                requests_by_endpoint[endpoint],
            )
            for endpoint, request_ids in self.augmented_request_ids_by_endpoint.items()
            if requests_by_endpoint.get(endpoint, 0) > 0
        }

    def _cache_hit_rates(self, requests_by_endpoint: dict[str, int]) -> dict[str, float]:
        rates: dict[str, float] = {}
        endpoints = (
            set(self.cache_hits_by_endpoint)
            | set(requests_by_endpoint)
            | set(self.stale_by_endpoint)
        )
        for endpoint in endpoints:
            total = (
                requests_by_endpoint.get(endpoint, 0)
                + self.cache_hits_by_endpoint.get(endpoint, 0)
                + self.stale_by_endpoint.get(endpoint, 0)
            )
            if total:
                rates[endpoint] = _ratio(self.cache_hits_by_endpoint.get(endpoint, 0), total)
        return rates


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def _as_float(value: object) -> float:
    return float(str(value))


def _as_int(value: object) -> int:
    return int(str(value))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _means(groups: dict[str, list[float]]) -> dict[str, float]:
    return {name: round(_mean(values), 1) for name, values in groups.items() if values}


def _latency_percentiles(
    groups: dict[str, list[float]],
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    percentiles: tuple[dict[str, float], dict[str, float], dict[str, float]] = ({}, {}, {})
    for provider, values in groups.items():
        if not values:
            continue
        ordered = sorted(values)
        for output, percentile in zip(percentiles, (0.50, 0.95, 0.99), strict=True):
            index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
            output[provider] = round(ordered[index], 1)
    return percentiles
