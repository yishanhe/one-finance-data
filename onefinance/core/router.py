"""Provider router — tier walking with cooldown logic.

The router consults a config-driven tier table to select which provider
to try first for each endpoint.  When a provider fails or hits a rate
limit, the router marks it in cooldown and tries the next tier.

See design doc §6 (tier strategy), §7 (quota handling).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from onefinance.audit.models import AuditEntry
from onefinance.core.config import OneFinanceConfig
from onefinance.core.errors import (
    AllProvidersFailedError,
    FinanceError,
    InvalidArgumentError,
    NotSupportedError,
    RateLimitError,
)
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)


@dataclass
class ProviderState:
    """Tracks cooldown and failure state for a single provider.

    See design doc §7 — state is in-memory only, does not persist
    across process restarts.

    Attributes
    ----------
    name:
        Provider identifier (e.g. ``"fmp"``).
    cooldown_until:
        Unix timestamp; 0 means available.
    last_error:
        Description of the most recent error, if any.
    consecutive_failures:
        Number of consecutive failures — drives exponential backoff.
    """

    name: str
    cooldown_until: float = 0.0
    last_error: str | None = None
    consecutive_failures: int = 0

    @property
    def is_available(self) -> bool:
        """True if the provider is not currently in cooldown."""
        return time.time() >= self.cooldown_until

    @property
    def cooldown_remaining(self) -> float:
        """Seconds remaining in cooldown (0 if available)."""
        return max(0.0, self.cooldown_until - time.time())

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
        """Put the provider into cooldown after a failure.

        Uses exponential backoff: ``min(cooldown_seconds × 2^failures, max_backoff)``.
        """
        self.consecutive_failures += 1
        self.last_error = error

        backoff = min(
            cooldown_seconds * (2 ** (self.consecutive_failures - 1)),
            max_backoff,
        )
        self.cooldown_until = time.time() + backoff

        logger.info(
            "Provider %s in cooldown for %.0fs (failure #%d: %s)",
            self.name,
            backoff,
            self.consecutive_failures,
            error,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialisable snapshot for diagnostics."""
        return {
            "name": self.name,
            "available": self.is_available,
            "cooldown_remaining_s": round(self.cooldown_remaining, 1),
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }


class ProviderRouter:
    """Selects providers using config-driven tier lists with cooldown handling.

    Parameters
    ----------
    providers:
        Map of provider name → provider instance.
    config:
        Full OneFinance config (tier tables, cooldown settings).
    """

    def __init__(
        self,
        providers: dict[str, BaseProvider],
        config: OneFinanceConfig | None = None,
        audit_log: Any | None = None,
    ) -> None:
        self._providers = providers
        self._config = config or OneFinanceConfig()
        self._cooldown_config = self._config.cooldown
        self._audit = audit_log

        # Initialise per-provider state
        self._state: dict[str, ProviderState] = {
            name: ProviderState(name=name) for name in providers
        }

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def dispatch(
        self,
        endpoint: str,
        fetch_fn: Callable[[BaseProvider], Any],
        *,
        fresh: bool = False,
        provider_name: str | None = None,
    ) -> Any:
        """Route a request through the tier list, handling cooldowns.

        Parameters
        ----------
        endpoint:
            Endpoint name (e.g. ``"price_history"``, ``"quote"``).
        fetch_fn:
            Callable that takes a provider and returns the result.
        fresh:
            For Type C endpoints, whether fresh data is requested.
        provider_name:
            If set, force this specific provider (bypass tier list).

        Returns
        -------
        The result from the first successful provider.

        Raises
        ------
        AllProvidersFailedError
            When every tier has been tried and all failed.
        InvalidArgumentError
            When a forced provider name is not found.
        """
        providers = self._select_providers(endpoint, fresh=fresh, provider_name=provider_name)

        request_id = uuid.uuid4().hex[:12]
        tier_total = len(providers)
        failures: list[tuple[str, FinanceError]] = []
        providers_in_cooldown: list[str] = []

        for tier_pos, prov in enumerate(providers):
            state = self._state.get(prov.name)

            # Skip providers in cooldown (unless explicitly forced)
            if state and not state.is_available and provider_name is None:
                logger.debug(
                    "Skipping %s (cooldown, %.0fs remaining)",
                    prov.name,
                    state.cooldown_remaining,
                )
                providers_in_cooldown.append(prov.name)
                self._record_audit(
                    request_id=request_id,
                    endpoint=endpoint,
                    provider=prov.name,
                    status="skipped",
                    latency_ms=0.0,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                    error_message=f"cooldown, {state.cooldown_remaining:.0f}s remaining",
                )
                continue

            t0 = time.perf_counter()
            try:
                result = fetch_fn(prov)
                latency = (time.perf_counter() - t0) * 1000

                # Success → reset failure state
                if state:
                    state.mark_success()

                self._record_audit(
                    request_id=request_id,
                    endpoint=endpoint,
                    provider=prov.name,
                    status="success",
                    latency_ms=latency,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                )
                return result

            except NotSupportedError:
                latency = (time.perf_counter() - t0) * 1000
                # Silently skip — this provider doesn't support the endpoint
                logger.debug("%s does not support %s, skipping", prov.name, endpoint)
                self._record_audit(
                    request_id=request_id,
                    endpoint=endpoint,
                    provider=prov.name,
                    status="not_supported",
                    latency_ms=latency,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                )
                continue

            except RateLimitError as exc:
                latency = (time.perf_counter() - t0) * 1000
                logger.warning(
                    "Provider %s rate-limited for %s: %s",
                    prov.name,
                    endpoint,
                    exc.message,
                )
                # Mark cooldown using provider's cooldown hint
                cooldown_s = exc.retry_after_seconds or self._cooldown_config.default_initial_s
                if state:
                    state.mark_failure(
                        exc.message,
                        cooldown_s,
                        max_backoff=self._cooldown_config.max_backoff_s,
                    )
                failures.append((prov.name, exc))
                self._record_audit(
                    request_id=request_id,
                    endpoint=endpoint,
                    provider=prov.name,
                    status="rate_limited",
                    latency_ms=latency,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                    error_code=exc.code,
                    error_message=exc.message,
                )
                continue

            except FinanceError as exc:
                latency = (time.perf_counter() - t0) * 1000
                logger.warning(
                    "Provider %s failed for %s: %s",
                    prov.name,
                    endpoint,
                    exc.message,
                )
                # Mark cooldown with default initial backoff
                if state:
                    state.mark_failure(
                        exc.message,
                        self._cooldown_config.default_initial_s,
                        max_backoff=self._cooldown_config.max_backoff_s,
                    )
                failures.append((prov.name, exc))
                self._record_audit(
                    request_id=request_id,
                    endpoint=endpoint,
                    provider=prov.name,
                    status="error",
                    latency_ms=latency,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                    error_code=exc.code,
                    error_message=exc.message,
                )
                continue

        # All exhausted — include cooldown providers as potential fallbacks
        raise AllProvidersFailedError(
            endpoint=endpoint,
            failures=failures,
            fallback_providers_available=providers_in_cooldown,
        )

    # -------------------------------------------------------------------
    # State inspection
    # -------------------------------------------------------------------

    def state(self) -> dict[str, dict[str, Any]]:
        """Return the current state of all providers.

        Useful for diagnostics and the ``providers status`` CLI command.
        """
        return {name: s.to_dict() for name, s in self._state.items()}

    def get_provider_state(self, name: str) -> ProviderState | None:
        """Return the state for a specific provider."""
        return self._state.get(name)

    def reset_cooldowns(self) -> None:
        """Clear all provider cooldowns (useful for testing)."""
        for s in self._state.values():
            s.cooldown_until = 0.0
            s.last_error = None
            s.consecutive_failures = 0

    # -------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------

    def _select_providers(
        self,
        endpoint: str,
        *,
        fresh: bool = False,
        provider_name: str | None = None,
    ) -> list[BaseProvider]:
        """Build the ordered list of providers to try.

        If *provider_name* is set, returns only that provider.
        Otherwise returns providers matching the tier list for
        *endpoint*, filtered to those actually registered.
        """
        if provider_name:
            prov = self._providers.get(provider_name)
            if prov is None:
                raise InvalidArgumentError(
                    f"Provider '{provider_name}' not found. "
                    f"Available: {list(self._providers.keys())}"
                )
            return [prov]

        tier_list = self._config.get_tier_list(endpoint, fresh=fresh)

        if not tier_list:
            # No tier config for this endpoint — fall back to all providers
            logger.debug("No tier config for %s, using all providers", endpoint)
            return list(self._providers.values())

        # Map tier names to provider instances (skip missing)
        providers: list[BaseProvider] = []
        for name in tier_list:
            prov = self._providers.get(name)
            if prov is not None:
                providers.append(prov)

        if not providers:
            # None of the tier-listed providers are registered
            logger.warning(
                "None of the tier-listed providers for %s are registered: %s",
                endpoint,
                tier_list,
            )
            return list(self._providers.values())

        return providers

    def _record_audit(
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
    ) -> None:
        """Record an audit entry if audit log is enabled."""
        if self._audit is None:
            return
        from datetime import datetime

        try:
            self._audit.record(
                AuditEntry(
                    timestamp=datetime.now(UTC),
                    request_id=request_id,
                    endpoint=endpoint,
                    provider=provider,
                    status=status,
                    latency_ms=latency_ms,
                    tier_position=tier_position,
                    tier_total=tier_total,
                    error_code=error_code,
                    error_message=error_message,
                )
            )
        except Exception:
            # Never let audit logging break the data flow
            logger.debug("Failed to record audit entry", exc_info=True)
