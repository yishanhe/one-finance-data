"""AuditRecorder — single construction point for ``AuditEntry`` rows.

Both ``OneFinanceClient`` (cache-hit path) and ``ProviderRouter`` (every
dispatch attempt) previously built ``AuditEntry`` instances inline. This
module centralises that construction so the row shape stays consistent and
the wall-clock timestamp is always sourced from the active clock.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from onefinance._clock import get_clock
from onefinance.audit.models import AuditEntry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Stable request metadata shared by all audit rows for one logical call."""

    request_id: str
    endpoint: str
    symbol: str | None = None
    cache_key: str | None = None

    @classmethod
    def new(
        cls,
        endpoint: str,
        *,
        request_id: str | None = None,
        symbol: str | None = None,
        cache_key: str | None = None,
    ) -> AuditContext:
        """Create audit metadata for one logical request."""
        return cls(
            request_id=request_id or uuid.uuid4().hex[:12],
            endpoint=endpoint,
            symbol=symbol,
            cache_key=cache_key,
        )

    def derive(
        self,
        *,
        symbol: str | None = None,
        cache_key: str | None = None,
    ) -> AuditContext:
        """Return related audit metadata with the same request id and endpoint."""
        return AuditContext(
            request_id=self.request_id,
            endpoint=self.endpoint,
            symbol=symbol,
            cache_key=cache_key,
        )


class AuditSink(Protocol):
    """Minimal audit-log surface required by ``AuditRecorder``."""

    @property
    def enabled(self) -> bool: ...

    def record(self, entry: AuditEntry) -> None: ...


class AuditRecorder:
    """Wraps :class:`AuditLog` and constructs ``AuditEntry`` rows."""

    def __init__(self, audit_log: AuditSink | None) -> None:
        self._audit = audit_log

    @property
    def enabled(self) -> bool:
        return self._audit is not None and getattr(self._audit, "enabled", True)

    def record_cache_hit(
        self,
        *,
        context: AuditContext,
    ) -> None:
        self._record(
            context=context,
            provider="cache",
            status="cache_hit",
            latency_ms=0.0,
        )

    def record_success(
        self,
        *,
        context: AuditContext,
        provider: str,
        latency_ms: float,
        tier_position: int,
        tier_total: int,
        is_fallback: bool = False,
    ) -> None:
        self._record(
            context=context,
            provider=provider,
            status="success",
            latency_ms=latency_ms,
            tier_position=tier_position,
            tier_total=tier_total,
            is_fallback=is_fallback,
        )

    def record_not_supported(
        self,
        *,
        context: AuditContext,
        provider: str,
        latency_ms: float,
        tier_position: int,
        tier_total: int,
        http_status: int | None = None,
    ) -> None:
        self._record(
            context=context,
            provider=provider,
            status="not_supported",
            latency_ms=latency_ms,
            tier_position=tier_position,
            tier_total=tier_total,
            http_status=http_status,
        )

    def record_skipped(
        self,
        *,
        context: AuditContext,
        provider: str,
        tier_position: int,
        tier_total: int,
        reason: str,
    ) -> None:
        self._record(
            context=context,
            provider=provider,
            status="skipped",
            latency_ms=0.0,
            tier_position=tier_position,
            tier_total=tier_total,
            error_message=reason,
        )

    def record_stale_serve(
        self,
        *,
        context: AuditContext,
        stale_age_s: float | None = None,
    ) -> None:
        """Record that a last-known-good (stale) copy was served after all providers failed."""
        self._record(
            context=context,
            provider="cache",
            status="stale",
            latency_ms=0.0,
            stale_age_s=stale_age_s,
        )

    def record_augment(
        self,
        *,
        context: AuditContext,
        provider: str,
        latency_ms: float,
        tier_position: int,
        tier_total: int,
    ) -> None:
        self._record(
            context=context,
            provider=provider,
            status="augment",
            latency_ms=latency_ms,
            tier_position=tier_position,
            tier_total=tier_total,
        )

    def record_augment_cache_hit(self, *, context: AuditContext) -> None:
        """Record reuse of a cached augment filler without a provider call."""
        self._record(
            context=context,
            provider="cache",
            status="augment_cache_hit",
            latency_ms=0.0,
        )

    def record_failure(
        self,
        *,
        context: AuditContext,
        provider: str,
        latency_ms: float,
        tier_position: int,
        tier_total: int,
        error_code: str,
        error_message: str,
        rate_limited: bool,
        http_status: int | None = None,
        is_fallback: bool = False,
    ) -> None:
        self._record(
            context=context,
            provider=provider,
            status="rate_limited" if rate_limited else "error",
            latency_ms=latency_ms,
            tier_position=tier_position,
            tier_total=tier_total,
            error_code=error_code,
            error_message=error_message,
            http_status=http_status,
            is_fallback=is_fallback,
        )

    def record_all_failed(
        self,
        *,
        context: AuditContext,
        tier_total: int,
        error_message: str,
    ) -> None:
        """Terminal row: every provider skipped or failed for this request.

        Without this row a request that exhausts the tier leaves only
        per-provider ``skipped``/``error`` rows — the request-level outcome is
        invisible to ``audit stats``. Recorded just before
        ``AllProvidersFailedError`` propagates; when the client then serves a
        last-known-good copy, a ``stale`` row follows under the same
        request_id.
        """
        self._record(
            context=context,
            provider="router",
            status="all_failed",
            latency_ms=0.0,
            tier_position=tier_total,
            tier_total=tier_total,
            error_code="ALL_PROVIDERS_FAILED",
            error_message=error_message,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _record(
        self,
        *,
        context: AuditContext,
        provider: str,
        status: str,
        latency_ms: float,
        tier_position: int = 0,
        tier_total: int = 1,
        error_code: str | None = None,
        error_message: str | None = None,
        http_status: int | None = None,
        is_fallback: bool = False,
        stale_age_s: float | None = None,
    ) -> None:
        if not self.enabled or self._audit is None:
            return
        try:
            self._audit.record(
                AuditEntry(
                    timestamp=get_clock().now(),
                    request_id=context.request_id,
                    endpoint=context.endpoint,
                    provider=provider,
                    symbol=context.symbol,
                    status=status,
                    latency_ms=latency_ms,
                    tier_position=tier_position,
                    tier_total=tier_total,
                    error_code=error_code,
                    error_message=error_message,
                    cache_key=context.cache_key,
                    http_status=http_status,
                    is_fallback=is_fallback,
                    stale_age_s=stale_age_s,
                )
            )
        except Exception:
            # Never let audit logging break the data flow.
            logger.debug("Failed to record audit entry", exc_info=True)
