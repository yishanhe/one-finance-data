"""Provider router — tier walking with cooldown logic.

The router consults a config-driven tier table to select which provider
to try first for each endpoint.  When a provider fails or hits a rate
limit, the router marks it in cooldown and tries the next tier.

See design doc §6 (tier strategy), §7 (quota handling).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from onefinance._clock import get_clock
from onefinance.audit._recorder import AuditRecorder
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
        return get_clock().time() >= self.cooldown_until

    @property
    def cooldown_remaining(self) -> float:
        """Seconds remaining in cooldown (0 if available)."""
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
        """Put the provider into cooldown after a failure.

        Uses exponential backoff: ``min(cooldown_seconds × 2^failures, max_backoff)``.
        """
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
        self._audit = AuditRecorder(audit_log)

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

            if state and not state.is_available and provider_name is None:
                logger.debug(
                    "Skipping %s (cooldown, %.0fs remaining)",
                    prov.name,
                    state.cooldown_remaining,
                )
                providers_in_cooldown.append(prov.name)
                self._audit.record_skipped(
                    request_id=request_id,
                    endpoint=endpoint,
                    provider=prov.name,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                    reason=f"cooldown, {state.cooldown_remaining:.0f}s remaining",
                )
                continue

            t0 = get_clock().perf_counter()
            try:
                result = fetch_fn(prov)
                latency = (get_clock().perf_counter() - t0) * 1000
                if state:
                    state.mark_success()
                self._audit.record_success(
                    request_id=request_id,
                    endpoint=endpoint,
                    provider=prov.name,
                    latency_ms=latency,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                )
                return result

            except NotSupportedError:
                latency = (get_clock().perf_counter() - t0) * 1000
                logger.debug("%s does not support %s, skipping", prov.name, endpoint)
                self._audit.record_not_supported(
                    request_id=request_id,
                    endpoint=endpoint,
                    provider=prov.name,
                    latency_ms=latency,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                )
                continue

            except (RateLimitError, FinanceError) as exc:
                latency = (get_clock().perf_counter() - t0) * 1000
                self._handle_provider_failure(
                    exc=exc,
                    state=state,
                    request_id=request_id,
                    endpoint=endpoint,
                    provider=prov.name,
                    latency_ms=latency,
                    tier_pos=tier_pos,
                    tier_total=tier_total,
                )
                failures.append((prov.name, exc))
                continue

        raise AllProvidersFailedError(
            endpoint=endpoint,
            failures=failures,
            fallback_providers_available=providers_in_cooldown,
        )

    def _handle_provider_failure(
        self,
        *,
        exc: FinanceError,
        state: ProviderState | None,
        request_id: str,
        endpoint: str,
        provider: str,
        latency_ms: float,
        tier_pos: int,
        tier_total: int,
    ) -> None:
        """Single audit + cooldown bookkeeping path for any provider failure."""
        rate_limited = isinstance(exc, RateLimitError)
        cooldown_s = (
            exc.retry_after_seconds
            if rate_limited and exc.retry_after_seconds
            else self._cooldown_config.default_initial_s
        )
        log = logger.warning
        log(
            "Provider %s %s for %s: %s",
            provider,
            "rate-limited" if rate_limited else "failed",
            endpoint,
            exc.message,
        )
        if state:
            state.mark_failure(
                exc.message,
                cooldown_s,
                max_backoff=self._cooldown_config.max_backoff_s,
            )
        self._audit.record_failure(
            request_id=request_id,
            endpoint=endpoint,
            provider=provider,
            latency_ms=latency_ms,
            tier_position=tier_pos,
            tier_total=tier_total,
            error_code=exc.code,
            error_message=exc.message,
            rate_limited=rate_limited,
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
        Otherwise returns providers matching the tier list for *endpoint*,
        filtered to those actually registered. Falls back to every
        registered provider in declaration order if the tier list is empty
        or unresolvable.
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
        resolved = [self._providers[n] for n in tier_list if n in self._providers]
        if resolved:
            return resolved

        if tier_list:
            logger.warning(
                "None of the tier-listed providers for %s are registered: %s",
                endpoint,
                tier_list,
            )
        else:
            logger.debug("No tier config for %s, using all providers", endpoint)
        return list(self._providers.values())
