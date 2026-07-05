"""Provider router — tier walking with cooldown logic.

The router consults a config-driven tier table to select which provider
to try first for each endpoint.  When a provider fails or hits a rate
limit, the router marks it in cooldown and tries the next tier.

See design doc §6 (tier strategy), §7 (quota handling).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

from onefinance._clock import get_clock
from onefinance.audit._recorder import AuditContext, AuditRecorder, AuditSink
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

T = TypeVar("T")


class RouterCache(Protocol):
    """Cache capabilities used by ``ProviderRouter``.

    The router intentionally does not depend on the full ``CacheManager`` API;
    it only needs negative-cache, augment-cache, and cooldown-state helpers.
    """

    def get_negative_global(self, provider: str, endpoint: str) -> bool: ...

    def get_negative(self, provider: str, endpoint: str, symbol: str | None) -> bool: ...

    def set_negative_global(self, provider: str, endpoint: str, ttl: int = ...) -> None: ...

    def set_negative(
        self, provider: str, endpoint: str, symbol: str | None, ttl: int = ...
    ) -> None: ...

    def get_augment(self, endpoint: str, symbol: str) -> Any | None: ...

    def set_augment(self, endpoint: str, symbol: str, value: Any, ttl: int = ...) -> None: ...

    def get_router_state(self, provider: str) -> Mapping[str, object] | None: ...

    def set_router_state(
        self, provider: str, state: Mapping[str, object], ttl: int = ...
    ) -> None: ...


# ---------------------------------------------------------------------------
# Null-fill merge helpers
# ---------------------------------------------------------------------------


def _is_missing(val: Any) -> bool:
    """A value counts as missing if it is None or numerically zero."""
    if val is None:
        return True
    if isinstance(val, (int, float)) and val == 0:
        return True
    return False


def _merge_model(base: Any, filler: Any, fields: list[str]) -> Any:
    """Return *base* with missing fields filled from *filler*.

    Only fills fields that are missing in *base* and present in *filler*.
    When any field is filled, the ``source`` attribute is combined as
    ``"<base_source>+<filler_source>"``.  Returns *base* unchanged if
    no fields are filled.
    """
    updates: dict[str, Any] = {}
    for f in fields:
        if _is_missing(getattr(base, f, None)) and not _is_missing(getattr(filler, f, None)):
            updates[f] = getattr(filler, f)
    if not updates:
        return base
    base_src: str = getattr(base, "source", "")
    fill_src: str = getattr(filler, "source", "")
    if fill_src and fill_src not in base_src:
        updates["source"] = f"{base_src}+{fill_src}"
    return base.model_copy(update=updates)


@dataclass
class ProviderState:
    """Tracks cooldown and failure state for a single provider.

    See design doc §7 — the active state is held in memory and can be
    snapshotted through ``to_persisted_dict`` for cross-process cooldowns.

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

    def to_persisted_dict(self) -> dict[str, object]:
        """Minimal snapshot needed to restore cooldown state later."""
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


@dataclass(frozen=True, slots=True)
class _SkipDecision:
    reason: str
    include_as_cooldown_fallback: bool = False


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
        audit_log: AuditSink | None = None,
        cache: RouterCache | None = None,
    ) -> None:
        self._providers = providers
        self._config = config or OneFinanceConfig()
        self._cooldown_config = self._config.cooldown
        self._audit = AuditRecorder(audit_log)
        self._cache = cache  # CacheManager — used for negative (not_supported) caching

        self._state: dict[str, ProviderState] = {
            name: ProviderState(name=name) for name in providers
        }
        # P3: restore persisted cooldown state from diskcache (cross-process backoff)
        if self._cache is not None:
            for name, state in self._state.items():
                persisted = self._cache.get_router_state(name)
                if persisted:
                    state.restore_persisted(persisted)

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def dispatch(
        self,
        endpoint: str,
        fetch_fn: Callable[[BaseProvider], T],
        *,
        fresh: bool = False,
        provider_name: str | None = None,
        symbol: str | None = None,
        cache_key: str | None = None,
        context: AuditContext | None = None,
    ) -> T:
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
        context:
            Optional audit metadata for this logical request. When omitted,
            the router creates one from ``endpoint``, ``symbol``, and
            ``cache_key`` for direct callers/tests.

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

        audit_context = context or AuditContext.new(
            endpoint,
            symbol=symbol,
            cache_key=cache_key,
        )
        tier_total = len(providers)
        failures: list[tuple[str, FinanceError]] = []
        providers_in_cooldown: list[str] = []

        aug_cfg = self._config.augment
        aug_fields: list[str] = list(aug_cfg.fields.get(endpoint, [])) if aug_cfg.enabled else []

        for tier_pos, prov in enumerate(providers):
            state = self._state.get(prov.name)

            skip = self._skip_decision(
                prov,
                state,
                context=audit_context,
                forced=provider_name is not None,
            )
            if skip is not None:
                if skip.include_as_cooldown_fallback:
                    providers_in_cooldown.append(prov.name)
                self._audit.record_skipped(
                    context=audit_context,
                    provider=prov.name,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                    reason=skip.reason,
                )
                continue

            t0 = get_clock().perf_counter()
            try:
                result = fetch_fn(prov)
                latency = (get_clock().perf_counter() - t0) * 1000
                if state:
                    state.mark_success()
                self._audit.record_success(
                    context=audit_context,
                    provider=prov.name,
                    latency_ms=latency,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                    is_fallback=len(failures) > 0,
                )

                if aug_fields and hasattr(result, "model_copy"):
                    missing = [f for f in aug_fields if _is_missing(getattr(result, f, None))]
                    if missing:
                        result = self._augment(
                            result=result,
                            missing_fields=missing,
                            all_aug_fields=aug_fields,
                            remaining_providers=providers[tier_pos + 1 :],
                            fetch_fn=fetch_fn,
                            context=audit_context,
                            tier_total=tier_total,
                        )

                return result

            except NotSupportedError as ns_exc:
                latency = (get_clock().perf_counter() - t0) * 1000
                self._handle_not_supported(
                    exc=ns_exc,
                    context=audit_context,
                    provider=prov.name,
                    latency_ms=latency,
                    tier_pos=tier_pos,
                    tier_total=tier_total,
                )
                continue

            except (RateLimitError, FinanceError) as exc:
                latency = (get_clock().perf_counter() - t0) * 1000
                self._handle_provider_failure(
                    exc=exc,
                    state=state,
                    context=audit_context,
                    provider=prov.name,
                    latency_ms=latency,
                    tier_pos=tier_pos,
                    tier_total=tier_total,
                    is_fallback=len(failures) > 0,
                )
                failures.append((prov.name, exc))
                continue

        raise AllProvidersFailedError(
            endpoint=endpoint,
            failures=failures,
            fallback_providers_available=providers_in_cooldown,
        )

    def _skip_decision(
        self,
        prov: BaseProvider,
        state: ProviderState | None,
        *,
        context: AuditContext,
        forced: bool,
    ) -> _SkipDecision | None:
        """Return why a provider should be skipped before a real attempt, if any."""
        if state and not state.is_available and not forced:
            logger.debug(
                "Skipping %s (cooldown, %.0fs remaining)",
                prov.name,
                state.cooldown_remaining,
            )
            return _SkipDecision(
                reason=f"cooldown, {state.cooldown_remaining:.0f}s remaining",
                include_as_cooldown_fallback=True,
            )

        # P4: plan-gated 402/403 errors apply to every symbol on that provider+endpoint pair.
        if self._cache is not None and self._cache.get_negative_global(prov.name, context.endpoint):
            logger.debug(
                "Skipping %s for %s (globally cached not_supported)",
                prov.name,
                context.endpoint,
            )
            return _SkipDecision(reason="globally cached not_supported (402/403)")

        if self._cache is not None and self._cache.get_negative(
            prov.name, context.endpoint, context.symbol
        ):
            logger.debug(
                "Skipping %s for %s/%s (cached not_supported)",
                prov.name,
                context.endpoint,
                context.symbol,
            )
            return _SkipDecision(reason="cached not_supported")

        return None

    def _augment(
        self,
        *,
        result: T,
        missing_fields: list[str],
        all_aug_fields: list[str],
        remaining_providers: list[BaseProvider],
        fetch_fn: Callable[[BaseProvider], T],
        context: AuditContext,
        tier_total: int,
    ) -> T:
        """Best-effort: fill missing fields in *result* from remaining providers.

        Skips providers in cooldown.  Swallows all exceptions — augment is
        opportunistic and must never block the primary result from returning.
        Does not update cooldown state for augment-specific failures.
        """
        current = result
        still_missing = list(missing_fields)

        # P2-A: check if we already have a recent augment result for this symbol.
        # Symbol is extracted from the result (Quote.symbol, etc.) if available.
        sym = getattr(result, "symbol", None)
        if sym and self._cache is not None:
            cached_aug = self._cache.get_augment(context.endpoint, sym)
            if cached_aug is not None:
                merged = _merge_model(current, cached_aug, all_aug_fields)
                if merged is not current:
                    logger.debug("Augment cache hit for %s/%s", context.endpoint, sym)
                    return cast(T, merged)

        for aug_idx, prov in enumerate(remaining_providers):
            if not still_missing:
                break
            state = self._state.get(prov.name)
            if state and not state.is_available:
                continue
            t0 = get_clock().perf_counter()
            try:
                aug_result = fetch_fn(prov)
                latency = (get_clock().perf_counter() - t0) * 1000
                merged = _merge_model(current, aug_result, all_aug_fields)
                if merged is not current:
                    aug_tier_pos = tier_total - len(remaining_providers) + aug_idx
                    self._audit.record_augment(
                        context=context,
                        provider=prov.name,
                        latency_ms=latency,
                        tier_position=aug_tier_pos,
                        tier_total=tier_total,
                    )
                    # P2-A: cache the raw augment result for ~5 min
                    if sym and self._cache is not None:
                        self._cache.set_augment(context.endpoint, sym, aug_result)
                    current = merged
                    still_missing = [
                        f for f in all_aug_fields if _is_missing(getattr(current, f, None))
                    ]
            except Exception:
                logger.debug(
                    "Augment provider %s skipped for %s",
                    prov.name,
                    context.endpoint,
                    exc_info=True,
                )

        return current

    def _handle_provider_failure(
        self,
        *,
        exc: FinanceError,
        state: ProviderState | None,
        context: AuditContext,
        provider: str,
        latency_ms: float,
        tier_pos: int,
        tier_total: int,
        is_fallback: bool = False,
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
            context.endpoint,
            exc.message,
        )
        if state:
            state.mark_failure(
                exc.message,
                cooldown_s,
                max_backoff=self._cooldown_config.max_backoff_s,
            )
            # P3: persist cooldown state to diskcache for cross-process backoff
            if self._cache is not None:
                self._cache.set_router_state(provider, state.to_persisted_dict())
        self._audit.record_failure(
            context=context,
            provider=provider,
            latency_ms=latency_ms,
            tier_position=tier_pos,
            tier_total=tier_total,
            error_code=exc.code,
            error_message=exc.message,
            rate_limited=rate_limited,
            http_status=getattr(exc, "http_status", None),
            is_fallback=is_fallback,
        )

    def _handle_not_supported(
        self,
        *,
        exc: NotSupportedError,
        context: AuditContext,
        provider: str,
        latency_ms: float,
        tier_pos: int,
        tier_total: int,
    ) -> None:
        """Audit unsupported endpoints and update negative-cache entries."""
        logger.debug("%s does not support %s, skipping", provider, context.endpoint)
        self._audit.record_not_supported(
            context=context,
            provider=provider,
            latency_ms=latency_ms,
            tier_position=tier_pos,
            tier_total=tier_total,
            http_status=exc.http_status,
        )
        if self._cache is None:
            return

        # P4: plan-gated 402/403 apply to all symbols. Per-symbol write still
        # happens so the symbol-specific check path also hits.
        if exc.http_status in {402, 403}:
            self._cache.set_negative_global(provider, context.endpoint)
        self._cache.set_negative(provider, context.endpoint, context.symbol)

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

        if not resolved:
            if tier_list:
                logger.warning(
                    "None of the tier-listed providers for %s are registered: %s",
                    endpoint,
                    tier_list,
                )
            else:
                logger.debug("No tier config for %s, using all providers", endpoint)
            resolved = list(self._providers.values())

        # Append fallback providers not already in the resolved list.
        # This ensures e.g. yfinance is always tried last even for endpoints
        # whose tier list doesn't include it.
        already_in_list = {p.name for p in resolved}
        for name in self._config.fallback_order:
            if name not in already_in_list:
                prov = self._providers.get(name)
                if prov is not None:
                    resolved.append(prov)
                    already_in_list.add(name)

        return resolved
