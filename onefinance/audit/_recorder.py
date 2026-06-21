"""AuditRecorder — single construction point for ``AuditEntry`` rows.

Both ``OneFinanceClient`` (cache-hit path) and ``ProviderRouter`` (every
dispatch attempt) previously built ``AuditEntry`` instances inline. This
module centralises that construction so the row shape stays consistent and
the wall-clock timestamp is always sourced from the active clock.
"""

from __future__ import annotations

import logging
from typing import Any

from onefinance._clock import get_clock
from onefinance.audit.models import AuditEntry

logger = logging.getLogger(__name__)


class AuditRecorder:
    """Wraps :class:`AuditLog` and constructs ``AuditEntry`` rows."""

    def __init__(self, audit_log: Any | None) -> None:
        self._audit = audit_log

    @property
    def enabled(self) -> bool:
        return self._audit is not None and getattr(self._audit, "enabled", True)

    def record_cache_hit(
        self,
        *,
        request_id: str,
        endpoint: str,
        cache_key: str,
        symbol: str | None = None,
    ) -> None:
        self._record(
            request_id=request_id,
            endpoint=endpoint,
            provider="cache",
            status="cache_hit",
            latency_ms=0.0,
            cache_key=cache_key,
            symbol=symbol,
        )

    def record_success(
        self,
        *,
        request_id: str,
        endpoint: str,
        provider: str,
        latency_ms: float,
        tier_position: int,
        tier_total: int,
        symbol: str | None = None,
        is_fallback: bool = False,
    ) -> None:
        self._record(
            request_id=request_id,
            endpoint=endpoint,
            provider=provider,
            status="success",
            latency_ms=latency_ms,
            tier_position=tier_position,
            tier_total=tier_total,
            symbol=symbol,
            is_fallback=is_fallback,
        )

    def record_not_supported(
        self,
        *,
        request_id: str,
        endpoint: str,
        provider: str,
        latency_ms: float,
        tier_position: int,
        tier_total: int,
        symbol: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self._record(
            request_id=request_id,
            endpoint=endpoint,
            provider=provider,
            status="not_supported",
            latency_ms=latency_ms,
            tier_position=tier_position,
            tier_total=tier_total,
            symbol=symbol,
            http_status=http_status,
        )

    def record_skipped(
        self,
        *,
        request_id: str,
        endpoint: str,
        provider: str,
        tier_position: int,
        tier_total: int,
        reason: str,
        symbol: str | None = None,
    ) -> None:
        self._record(
            request_id=request_id,
            endpoint=endpoint,
            provider=provider,
            status="skipped",
            latency_ms=0.0,
            tier_position=tier_position,
            tier_total=tier_total,
            error_message=reason,
            symbol=symbol,
        )

    def record_stale_serve(
        self,
        *,
        request_id: str,
        endpoint: str,
        cache_key: str,
        symbol: str | None = None,
    ) -> None:
        """Record that a last-known-good (stale) copy was served after all providers failed."""
        self._record(
            request_id=request_id,
            endpoint=endpoint,
            provider="cache",
            status="stale",
            latency_ms=0.0,
            cache_key=cache_key,
            symbol=symbol,
        )

    def record_augment(
        self,
        *,
        request_id: str,
        endpoint: str,
        provider: str,
        latency_ms: float,
        tier_position: int,
        tier_total: int,
        symbol: str | None = None,
    ) -> None:
        self._record(
            request_id=request_id,
            endpoint=endpoint,
            provider=provider,
            status="augment",
            latency_ms=latency_ms,
            tier_position=tier_position,
            tier_total=tier_total,
            symbol=symbol,
        )

    def record_failure(
        self,
        *,
        request_id: str,
        endpoint: str,
        provider: str,
        latency_ms: float,
        tier_position: int,
        tier_total: int,
        error_code: str,
        error_message: str,
        rate_limited: bool,
        symbol: str | None = None,
        http_status: int | None = None,
        is_fallback: bool = False,
    ) -> None:
        self._record(
            request_id=request_id,
            endpoint=endpoint,
            provider=provider,
            status="rate_limited" if rate_limited else "error",
            latency_ms=latency_ms,
            tier_position=tier_position,
            tier_total=tier_total,
            error_code=error_code,
            error_message=error_message,
            symbol=symbol,
            http_status=http_status,
            is_fallback=is_fallback,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _record(
        self,
        *,
        request_id: str,
        endpoint: str,
        provider: str,
        status: str,
        latency_ms: float,
        tier_position: int = 0,
        tier_total: int = 1,
        error_code: str | None = None,
        error_message: str | None = None,
        cache_key: str | None = None,
        symbol: str | None = None,
        http_status: int | None = None,
        is_fallback: bool = False,
    ) -> None:
        if not self.enabled or self._audit is None:
            return
        try:
            self._audit.record(
                AuditEntry(
                    timestamp=get_clock().now(),
                    request_id=request_id,
                    endpoint=endpoint,
                    provider=provider,
                    symbol=symbol,
                    status=status,
                    latency_ms=latency_ms,
                    tier_position=tier_position,
                    tier_total=tier_total,
                    error_code=error_code,
                    error_message=error_message,
                    cache_key=cache_key,
                    http_status=http_status,
                    is_fallback=is_fallback,
                )
            )
        except Exception:
            # Never let audit logging break the data flow.
            logger.debug("Failed to record audit entry", exc_info=True)
