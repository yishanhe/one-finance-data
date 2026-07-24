"""State and persistence boundary for provider routing."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from onefinance._clock import get_clock

logger = logging.getLogger(__name__)


class RouterCache(Protocol):
    """Narrow cache capabilities required by provider routing."""

    def get_negative_global(self, provider: str, endpoint: str) -> bool: ...

    def get_negative(self, provider: str, endpoint: str, symbol: str | None) -> bool: ...

    def set_negative_global(self, provider: str, endpoint: str, ttl: int = ...) -> None: ...

    def set_negative(
        self, provider: str, endpoint: str, symbol: str | None, ttl: int = ...
    ) -> None: ...

    def get_endpoint_ok(self, provider: str, endpoint: str) -> bool: ...

    def mark_endpoint_ok(self, provider: str, endpoint: str, ttl: int = ...) -> None: ...

    def get_augment(self, endpoint: str, symbol: str) -> Any | None: ...

    def set_augment(self, endpoint: str, symbol: str, value: Any, ttl: int = ...) -> None: ...

    def get_router_state(self, provider: str) -> Mapping[str, object] | None: ...

    def set_router_state(
        self, provider: str, state: Mapping[str, object], ttl: int = ...
    ) -> None: ...


@dataclass
class ProviderState:
    """Cooldown and failure state for one provider.

    Active state lives in memory; ``to_persisted_dict`` exposes the minimal
    cross-process representation without leaking diagnostic-only fields.
    """

    name: str
    cooldown_until: float = 0.0
    last_error: str | None = None
    consecutive_failures: int = 0

    @property
    def is_available(self) -> bool:
        """True when the provider is not currently in cooldown."""
        return get_clock().time() >= self.cooldown_until

    @property
    def cooldown_remaining(self) -> float:
        """Seconds remaining in cooldown, or zero when available."""
        return max(0.0, self.cooldown_until - get_clock().time())

    def mark_success(self) -> None:
        """Reset failure state after a successful call."""
        self.cooldown_until = 0.0
        self.last_error = None
        self.consecutive_failures = 0

    def mark_failure(
        self,
        error: str,
        cooldown_seconds: float,
        *,
        max_backoff: float = 3600.0,
    ) -> None:
        """Enter cooldown using bounded exponential backoff."""
        self.consecutive_failures += 1
        self.last_error = error
        backoff = min(
            cooldown_seconds * (2 ** (self.consecutive_failures - 1)),
            max_backoff,
        )
        self.cooldown_until = get_clock().time() + backoff
        logger.info(
            "Provider %s in cooldown for %.0fs (failure #%d: %s)",
            self.name,
            backoff,
            self.consecutive_failures,
            error,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a diagnostic snapshot."""
        return {
            "name": self.name,
            "available": self.is_available,
            "cooldown_remaining_s": round(self.cooldown_remaining, 1),
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }

    def to_persisted_dict(self) -> dict[str, object]:
        """Return the minimal state required to restore cooldown later."""
        return {
            "cooldown_until": self.cooldown_until,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }

    def restore_persisted(self, data: Mapping[str, object]) -> None:
        """Restore cooldown state from a persisted snapshot."""
        cooldown_until = data.get("cooldown_until", 0.0)
        consecutive_failures = data.get("consecutive_failures", 0)
        last_error = data.get("last_error")

        self.cooldown_until = (
            float(cooldown_until) if isinstance(cooldown_until, int | float) else 0.0
        )
        self.consecutive_failures = (
            int(consecutive_failures) if isinstance(consecutive_failures, int | float) else 0
        )
        self.last_error = last_error if isinstance(last_error, str) else None
