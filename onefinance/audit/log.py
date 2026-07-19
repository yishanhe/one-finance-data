"""JSONL file-backed audit log for provider API calls.

Stores one JSON object per line (append-only).  Designed for low
overhead (single line write per API call), human-readability
(``cat audit.jsonl | jq``), and programmatic querying via the
``query()`` and ``stats()`` helpers.

Default location: ``~/.one_finance_data/audit/audit.jsonl``.
Default retention: 30 days (pruned on startup).
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from onefinance._clock import get_clock
from onefinance.audit.models import AuditEntry, AuditStats

logger = logging.getLogger(__name__)

_DEFAULT_LOG_DIR = "~/.one_finance_data/audit"
_DEFAULT_RETENTION_DAYS = 30


class AuditLog:
    """JSONL file-backed audit log for provider API calls.

    Parameters
    ----------
    log_path:
        Path to the JSONL log file.  If a directory is given,
        ``audit.jsonl`` is appended.  Defaults to
        ``~/.one_finance_data/audit/audit.jsonl``.
    retention_days:
        Entries older than this are pruned on startup.
        Set to ``0`` to disable pruning.
    enabled:
        If ``False``, ``record()`` is a no-op.  Useful for tests
        or when audit overhead is unwanted.
    """

    def __init__(
        self,
        log_path: str | Path | None = None,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        self._retention_days = retention_days
        self._path: Path | None = None
        self._fh: TextIO | None = None

        if not enabled:
            return

        resolved = Path(log_path or _DEFAULT_LOG_DIR).expanduser()
        if resolved.is_dir() or not resolved.suffix:
            resolved = resolved / "audit.jsonl"
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._path = resolved

        # Auto-prune old entries
        if retention_days > 0 and self._path.exists():
            pruned = self._prune()
            if pruned > 0:
                logger.debug(
                    "Pruned %d audit entries older than %d days",
                    pruned,
                    retention_days,
                )

    # -------------------------------------------------------------------
    # Write
    # -------------------------------------------------------------------

    def record(self, entry: AuditEntry) -> None:
        """Append an audit entry as a single JSON line.

        No-op if the log is disabled. Keeps the file handle open across
        calls (line-buffered, so each line is still flushed immediately)
        instead of paying an open/close syscall pair per audit entry —
        this is on the hot path for every provider call.
        """
        if not self._enabled or self._path is None:
            return

        try:
            if self._fh is None:
                self._fh = open(self._path, "a", buffering=1)
            line = json.dumps(entry.to_dict(), separators=(",", ":"))
            self._fh.write(line + "\n")
        except Exception:
            logger.debug("Failed to write audit entry", exc_info=True)

    # -------------------------------------------------------------------
    # Query
    # -------------------------------------------------------------------

    def query(
        self,
        *,
        provider: str | None = None,
        endpoint: str | None = None,
        status: str | None = None,
        symbol: str | None = None,
        request_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Query audit entries with optional filters.

        Returns newest-first, up to ``limit`` entries.
        """
        if not self._enabled or self._path is None or not self._path.exists():
            return []

        entries: list[AuditEntry] = []
        for raw in self._read_lines():
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Apply filters
            if provider is not None and obj.get("provider") != provider:
                continue
            if endpoint is not None and obj.get("endpoint") != endpoint:
                continue
            if status is not None and obj.get("status") != status:
                continue
            if symbol is not None and (obj.get("symbol") or "").upper() != symbol.upper():
                continue
            if request_id is not None and obj.get("request_id") != request_id:
                continue
            if since is not None:
                ts = _parse_ts(obj.get("timestamp", ""))
                if ts is not None and ts < since:
                    continue

            entries.append(_dict_to_entry(obj))

        # Newest-first, limited
        entries.reverse()
        return entries[:limit]

    # -------------------------------------------------------------------
    # Aggregate stats
    # -------------------------------------------------------------------

    def stats(
        self,
        *,
        since: datetime | None = None,
    ) -> AuditStats:
        """Compute aggregate stats over a time range.

        Parameters
        ----------
        since:
            Start of the stats period.  Defaults to 24 hours ago.
        """
        if not self._enabled or self._path is None or not self._path.exists():
            return AuditStats()

        if since is None:
            since = get_clock().now() - timedelta(days=1)

        now = get_clock().now()

        _REAL_STATUSES = {"success", "error", "rate_limited"}

        total_calls = 0
        cache_hits = 0
        stale_serves = 0
        failed_requests = 0
        failed_by_endpoint: dict[str, int] = defaultdict(int)
        stale_ages: list[float] = []
        calls_by: dict[str, int] = defaultdict(int)
        errors_by: dict[str, int] = defaultdict(int)
        rate_limits_by: dict[str, int] = defaultdict(int)
        latencies_by: dict[str, list[float]] = defaultdict(list)
        calls_by_endpoint: dict[str, int] = defaultdict(int)
        errors_by_endpoint: dict[str, int] = defaultdict(int)
        not_supported_by: dict[str, int] = defaultdict(int)
        augment_calls = 0
        augment_by: dict[str, int] = defaultdict(int)
        augment_lats_by: dict[str, list[float]] = defaultdict(list)
        augmented_request_ids: set[str] = set()
        cache_hits_by_endpoint: dict[str, int] = defaultdict(int)
        stale_by_endpoint: dict[str, int] = defaultdict(int)
        # request_id → list of real-attempt dicts (for fallback grouping)
        by_request: dict[str, list[dict[str, object]]] = defaultdict(list)

        for raw in self._read_lines():
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            ts = _parse_ts(obj.get("timestamp", ""))
            if ts is not None and ts < since:
                continue

            status = obj.get("status", "")

            if status == "cache_hit":
                cache_hits += 1
                cache_hits_by_endpoint[str(obj.get("endpoint", "unknown"))] += 1
                continue

            # stale = served from last-known-good after all providers failed;
            # no provider HTTP call was made, so it must not count as a call.
            if status == "stale":
                stale_serves += 1
                stale_by_endpoint[str(obj.get("endpoint", "unknown"))] += 1
                age = obj.get("stale_age_s")
                if age is not None:
                    stale_ages.append(float(age))
                continue

                # skipped = cooldown bypass, not a real call
            if status == "skipped":
                continue

            # not_supported = provider plan/capability gap — count separately
            if status == "not_supported":
                not_supported_by[str(obj.get("provider", "unknown"))] += 1
                continue

            # all_failed = request-level terminal row (no provider HTTP call);
            # counts a request the client could not serve from any provider.
            if status == "all_failed":
                failed_requests += 1
                failed_by_endpoint[str(obj.get("endpoint", "unknown"))] += 1
                continue

            prov = obj.get("provider", "unknown")
            endpoint = obj.get("endpoint", "unknown")
            total_calls += 1
            calls_by[prov] += 1
            calls_by_endpoint[endpoint] += 1
            latencies_by[prov].append(float(obj.get("latency_ms", 0)))

            # augment = secondary null-fill enrichment call — a real API call
            # (already counted above), broken out so its overhead is visible.
            if status == "augment":
                augment_calls += 1
                augment_by[str(prov)] += 1
                augment_lats_by[str(prov)].append(float(obj.get("latency_ms", 0)))
                aug_rid = str(obj.get("request_id", ""))
                if aug_rid:
                    augmented_request_ids.add(aug_rid)

            if status == "error":
                errors_by[prov] += 1
                errors_by_endpoint[endpoint] += 1
            elif status == "rate_limited":
                rate_limits_by[prov] += 1
                errors_by[prov] += 1
                errors_by_endpoint[endpoint] += 1

            # Only primary/fallback attempts count for fallback detection.
            # Augment calls are secondary enrichment, not failure-driven fallbacks.
            rid = str(obj.get("request_id", ""))
            if rid and status in ("success", "error", "rate_limited"):
                by_request[rid].append(obj)

        # Fallback stats: group by request_id, real attempts only (already filtered above)
        primary_failures_by: dict[str, int] = defaultdict(int)
        fallback_success_by: dict[str, int] = defaultdict(int)
        fallback_failure_by: dict[str, int] = defaultdict(int)
        fallback_requests = 0
        requests_with_real_attempts = len(by_request)

        for attempts in by_request.values():
            attempts.sort(key=lambda a: int(a.get("tier_position") or 0))  # type: ignore[call-overload]
            if len(attempts) >= 2:
                fallback_requests += 1
            first = attempts[0]
            if first.get("status") in ("error", "rate_limited"):
                primary_failures_by[str(first.get("provider", "unknown"))] += 1
            for attempt in attempts[1:]:
                prov = str(attempt.get("provider", "unknown"))
                if attempt.get("status") == "success":
                    fallback_success_by[prov] += 1
                elif attempt.get("status") in ("error", "rate_limited"):
                    fallback_failure_by[prov] += 1

        # Cache-hit / stale rates are REQUEST-level metrics, not provider-attempt
        # metrics. A single client request that misses is served by one or more
        # provider attempts sharing one request_id — primary, fallback, and
        # augment (secondary enrichment) all count as the *same* miss. Using
        # total_calls (provider attempts) as the denominator lets one miss that
        # fell back or augmented register as several misses, understating the
        # rate. `requests_with_real_attempts` groups by request_id, so it is the
        # true count of provider-served requests (misses). This is why every
        # quote — finnhub primary + yfinance volume-augment on one request_id —
        # now counts as a single miss rather than two.
        total_requests = requests_with_real_attempts + cache_hits + stale_serves
        cache_hit_rate = cache_hits / total_requests if total_requests > 0 else 0.0
        stale_serve_rate = stale_serves / total_requests if total_requests > 0 else 0.0
        avg_stale_age_s = sum(stale_ages) / len(stale_ages) if stale_ages else 0.0
        max_stale_age_s = max(stale_ages) if stale_ages else 0.0
        fallback_rate = (
            fallback_requests / requests_with_real_attempts
            if requests_with_real_attempts > 0
            else 0.0
        )
        # Augment rate is request-level too: how many provider-served requests
        # needed at least one enrichment call. Intersect with by_request so an
        # augment row whose primary success fell outside the window is ignored.
        augment_rate = (
            len(augmented_request_ids & by_request.keys()) / requests_with_real_attempts
            if requests_with_real_attempts > 0
            else 0.0
        )
        avg_augment_latency = {
            prov: round(sum(lats) / len(lats), 1) for prov, lats in augment_lats_by.items() if lats
        }

        # Per-endpoint cache hit rate — same request-level denominator as the
        # global rate, scoped to each endpoint (one request_id = one miss).
        requests_by_endpoint: dict[str, int] = defaultdict(int)
        for attempts in by_request.values():
            requests_by_endpoint[str(attempts[0].get("endpoint", "unknown"))] += 1
        cache_hit_rate_by_endpoint: dict[str, float] = {}
        for ep in set(cache_hits_by_endpoint) | set(requests_by_endpoint) | set(stale_by_endpoint):
            ep_total = (
                requests_by_endpoint.get(ep, 0)
                + cache_hits_by_endpoint.get(ep, 0)
                + stale_by_endpoint.get(ep, 0)
            )
            if ep_total > 0:
                cache_hit_rate_by_endpoint[ep] = round(
                    cache_hits_by_endpoint.get(ep, 0) / ep_total, 3
                )

        avg_latency = {
            prov: round(sum(lats) / len(lats), 1) for prov, lats in latencies_by.items() if lats
        }

        # Latency percentiles per provider (nearest-rank on the sorted samples).
        # The pool is every real attempt — successes and failures alike — so
        # these answer "how slow is the provider when it responds at all",
        # including error/rate-limit latencies. Mean hides tail latency; p95/p99
        # surface it. With few samples p99==p95==max trivially (expected).
        def _pct(sorted_lats: list[float], p: float) -> float:
            n = len(sorted_lats)
            idx = max(0, min(n - 1, math.ceil(p * n) - 1))
            return sorted_lats[idx]

        lat_p50: dict[str, float] = {}
        lat_p95: dict[str, float] = {}
        lat_p99: dict[str, float] = {}
        for prov, lats in latencies_by.items():
            if not lats:
                continue
            ordered = sorted(lats)
            lat_p50[prov] = round(_pct(ordered, 0.50), 1)
            lat_p95[prov] = round(_pct(ordered, 0.95), 1)
            lat_p99[prov] = round(_pct(ordered, 0.99), 1)

        return AuditStats(
            total_calls=total_calls,
            cache_hits=cache_hits,
            cache_hit_rate=round(cache_hit_rate, 3),
            stale_serves=stale_serves,
            stale_serve_rate=round(stale_serve_rate, 3),
            avg_stale_age_s=round(avg_stale_age_s, 1),
            max_stale_age_s=round(max_stale_age_s, 1),
            calls_by_provider=dict(calls_by),
            errors_by_provider=dict(errors_by),
            avg_latency_ms_by_provider=avg_latency,
            latency_p50_ms_by_provider=lat_p50,
            latency_p95_ms_by_provider=lat_p95,
            latency_p99_ms_by_provider=lat_p99,
            rate_limits_by_provider=dict(rate_limits_by),
            calls_by_endpoint=dict(calls_by_endpoint),
            errors_by_endpoint=dict(errors_by_endpoint),
            primary_failures_by_provider=dict(primary_failures_by),
            fallback_requests=fallback_requests,
            fallback_rate=round(fallback_rate, 3),
            fallback_success_by_provider=dict(fallback_success_by),
            fallback_failure_by_provider=dict(fallback_failure_by),
            augment_calls=augment_calls,
            augment_rate=round(augment_rate, 3),
            augment_calls_by_provider=dict(augment_by),
            avg_augment_latency_ms_by_provider=avg_augment_latency,
            cache_hits_by_endpoint=dict(cache_hits_by_endpoint),
            cache_hit_rate_by_endpoint=cache_hit_rate_by_endpoint,
            not_supported_by_provider=dict(not_supported_by),
            failed_requests=failed_requests,
            failed_requests_by_endpoint=dict(failed_by_endpoint),
            period_start=since,
            period_end=now,
        )

    # -------------------------------------------------------------------
    # Maintenance
    # -------------------------------------------------------------------

    def _prune(self) -> int:
        """Remove entries older than ``retention_days``.

        Rewrites the file in-place, keeping only recent entries.
        Returns the number of entries removed.
        """
        if self._path is None or not self._path.exists():
            return 0

        cutoff = get_clock().now() - timedelta(days=self._retention_days)
        kept: list[str] = []
        pruned = 0

        for raw in self._read_lines():
            try:
                obj = json.loads(raw)
                ts = _parse_ts(obj.get("timestamp", ""))
                if ts is not None and ts < cutoff:
                    pruned += 1
                    continue
            except json.JSONDecodeError:
                pruned += 1
                continue
            kept.append(raw)

        if pruned > 0:
            with open(self._path, "w") as f:
                for line in kept:
                    f.write(line if line.endswith("\n") else line + "\n")

        return pruned

    def clear(self) -> None:
        """Remove all entries from the audit log."""
        self._close_fh()
        if self._path is not None and self._path.exists():
            self._path.write_text("")

    def close(self) -> None:
        """Close the open audit-log file handle, if any."""
        self._close_fh()

    def _close_fh(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                logger.debug("Failed to close audit log handle", exc_info=True)
            self._fh = None

    @property
    def enabled(self) -> bool:
        """Whether this audit log is active."""
        return self._enabled

    @property
    def path(self) -> Path | None:
        """Path to the log file, or None if disabled."""
        return self._path

    # -------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------

    def _read_lines(self) -> list[str]:
        """Read all lines from the log file."""
        if self._path is None or not self._path.exists():
            return []
        with open(self._path) as f:
            return [line.strip() for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO timestamp string to datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _dict_to_entry(obj: dict[str, Any]) -> AuditEntry:
    """Convert a JSON dict to an AuditEntry."""
    return AuditEntry(
        timestamp=_parse_ts(obj.get("timestamp", "")) or get_clock().now(),
        request_id=obj.get("request_id", ""),
        endpoint=obj.get("endpoint", ""),
        provider=obj.get("provider", ""),
        symbol=obj.get("symbol"),
        status=obj.get("status", ""),
        latency_ms=obj.get("latency_ms", 0),
        error_code=obj.get("error_code"),
        error_message=obj.get("error_message"),
        tier_position=obj.get("tier_position", 0),
        tier_total=obj.get("tier_total", 1),
        http_status=obj.get("http_status"),
        cache_key=obj.get("cache_key"),
        is_fallback=bool(obj.get("is_fallback", False)),
        stale_age_s=obj.get("stale_age_s"),
    )
