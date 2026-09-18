"""Provider router — tier walking with cooldown logic.

The router consults a config-driven tier table to select which provider
to try first for each endpoint.  When a provider fails or hits a rate
limit, the router marks it in cooldown and tries the next tier.

See design doc §6 (tier strategy), §7 (quota handling).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from onefinance._clock import get_clock
from onefinance.audit._recorder import AuditContext, AuditRecorder, AuditSink
from onefinance.core._augmentation import ResultAugmenter
from onefinance.core._router_state import (
    ProviderState as ProviderState,
)
from onefinance.core._router_state import (
    RouterCache as RouterCache,
)
from onefinance.core.config import OneFinanceConfig
from onefinance.core.errors import (
    AllProvidersFailedError,
    FinanceError,
    InvalidArgumentError,
    NotSupportedError,
    ProviderError,
    RateLimitError,
)
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _SkipDecision:
    reason: str
    include_as_cooldown_fallback: bool = False


@dataclass(frozen=True, slots=True)
class _ProviderAttempt(Generic[T]):
    """Outcome of one real provider call in a tier walk."""

    result: T | None = None
    failure: FinanceError | None = None
    unsupported_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.failure is None and self.unsupported_reason is None


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

        self._state: dict[tuple[str, str], ProviderState] = {}
        self._augmenter = ResultAugmenter(
            self._config.augment,
            self._cache,
            self._state,
            self._audit,
        )

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def endpoint_availability(self, endpoint: str, *, fresh: bool = False) -> dict[str, Any]:
        """Report routable candidates and observed restrictions without an API call."""
        candidates = self._select_providers(endpoint, fresh=fresh, provider_name=None)
        providers: list[dict[str, Any]] = []
        for provider in candidates:
            state = self._state_for(provider.name, endpoint)
            gated = bool(self._cache and self._cache.get_negative_global(provider.name, endpoint))
            status = (
                "plan_gated" if gated else "cooldown" if not state.is_available else "unverified"
            )
            providers.append(
                {
                    "provider": provider.name,
                    "status": status,
                    "retry_after_seconds": round(state.cooldown_remaining, 1)
                    if status == "cooldown"
                    else None,
                }
            )
        statuses = {row["status"] for row in providers}
        status = (
            "unverified"
            if "unverified" in statuses
            else "cooldown"
            if "cooldown" in statuses
            else "plan_gated"
            if providers
            else "no_provider"
        )
        return {"status": status, "providers": providers, "live_verified": False}

    def dispatch(
        self,
        endpoint: str,
        fetch_fn: Callable[[BaseProvider], T],
        *,
        fresh: bool = False,
        provider_name: str | None = None,
        augment: bool | None = None,
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
        unavailable: list[tuple[str, str]] = []

        # Populate all endpoint-specific states before augmentation inspects
        # providers later in the tier.
        states = {prov.name: self._state_for(prov.name, endpoint) for prov in providers}

        augment_fields = self._augmenter.fields_for(endpoint) if augment is not False else []

        for tier_pos, prov in enumerate(providers):
            state = states[prov.name]
            skip = self._skip_decision(
                prov,
                state,
                context=audit_context,
                forced=provider_name is not None,
            )
            if skip is not None:
                if skip.include_as_cooldown_fallback:
                    providers_in_cooldown.append(prov.name)
                unavailable.append((prov.name, skip.reason))
                self._audit.record_skipped(
                    context=audit_context,
                    provider=prov.name,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                    reason=skip.reason,
                )
                continue

            attempt = self._attempt_provider(
                provider=prov,
                state=state,
                remaining_providers=providers[tier_pos + 1 :],
                fetch_fn=fetch_fn,
                context=audit_context,
                tier_pos=tier_pos,
                tier_total=tier_total,
                augment_fields=augment_fields,
                is_fallback=bool(failures),
            )
            if attempt.succeeded:
                return cast(T, attempt.result)
            if attempt.failure is not None:
                failures.append((prov.name, attempt.failure))
            elif attempt.unsupported_reason is not None:
                unavailable.append((prov.name, attempt.unsupported_reason))

        details = [f"{name}: {error.message}" for name, error in failures]
        details.extend(f"{name}: {reason}" for name, reason in unavailable)

        self._audit.record_all_failed(
            context=audit_context,
            tier_total=tier_total,
            error_message="; ".join(details) or "no configured provider supports this endpoint",
        )
        raise AllProvidersFailedError(
            endpoint=endpoint,
            failures=failures,
            fallback_providers_available=providers_in_cooldown,
            unavailable_providers=unavailable,
        )

    def dispatch_batch(
        self,
        endpoint: str,
        symbols: list[str],
        fetch_fn: Callable[[BaseProvider, list[str]], list[T]],
        *,
        provider_name: str | None = None,
        context: AuditContext | None = None,
    ) -> dict[str, T]:
        """Fetch a batch while routing only still-missing symbols.

        A provider may return a valid partial response.  Keep those results and
        give the remaining symbols to later eligible providers.
        """
        providers = self._select_providers(endpoint, provider_name=provider_name)
        audit_context = context or AuditContext.new(endpoint)
        tier_total = len(providers)
        pending = list(dict.fromkeys(symbols))
        results: dict[str, T] = {}
        failures: list[tuple[str, FinanceError]] = []
        unavailable: list[tuple[str, str]] = []
        providers_in_cooldown: list[str] = []
        had_success = False
        states = {prov.name: self._state_for(prov.name, endpoint) for prov in providers}

        for tier_pos, prov in enumerate(providers):
            if not pending:
                break
            batch_context = audit_context.derive(symbol=",".join(pending))
            skip = self._skip_decision(
                prov,
                states[prov.name],
                context=batch_context,
                forced=provider_name is not None,
            )
            if skip is not None:
                if skip.include_as_cooldown_fallback:
                    providers_in_cooldown.append(prov.name)
                unavailable.append((prov.name, skip.reason))
                self._audit.record_skipped(
                    context=batch_context,
                    provider=prov.name,
                    tier_position=tier_pos,
                    tier_total=tier_total,
                    reason=skip.reason,
                )
                continue

            requested = pending

            def fetch_pending(p: BaseProvider) -> list[T]:
                return fetch_fn(p, requested)

            attempt = self._attempt_provider(
                provider=prov,
                state=states[prov.name],
                remaining_providers=providers[tier_pos + 1 :],
                fetch_fn=fetch_pending,
                context=batch_context,
                tier_pos=tier_pos,
                tier_total=tier_total,
                augment_fields=[],
                is_fallback=bool(failures or results),
            )
            if attempt.succeeded:
                had_success = True
                returned = cast(list[T], attempt.result)
                returned_by_symbol = {
                    str(getattr(item, "symbol")): item
                    for item in returned
                    if getattr(item, "symbol", None) in pending
                }
                results.update(returned_by_symbol)
                pending = [symbol for symbol in pending if symbol not in returned_by_symbol]
            elif attempt.failure is not None:
                failures.append((prov.name, attempt.failure))
            elif attempt.unsupported_reason is not None:
                unavailable.append((prov.name, attempt.unsupported_reason))

        if pending and not results and not had_success:
            details = [f"{name}: {error.message}" for name, error in failures]
            details.extend(f"{name}: {reason}" for name, reason in unavailable)
            self._audit.record_all_failed(
                context=audit_context,
                tier_total=tier_total,
                error_message="; ".join(details) or "no configured provider supports this endpoint",
            )
            raise AllProvidersFailedError(
                endpoint=endpoint,
                failures=failures,
                fallback_providers_available=providers_in_cooldown,
                unavailable_providers=unavailable,
            )
        return results

    def _attempt_provider(
        self,
        *,
        provider: BaseProvider,
        state: ProviderState | None,
        remaining_providers: list[BaseProvider],
        fetch_fn: Callable[[BaseProvider], T],
        context: AuditContext,
        tier_pos: int,
        tier_total: int,
        augment_fields: list[str],
        is_fallback: bool,
    ) -> _ProviderAttempt[T]:
        """Execute and account for one non-skipped provider attempt."""
        prefetch = self._augmenter.maybe_prefetch(
            primary=provider,
            fields=augment_fields,
            remaining_providers=remaining_providers,
            fetch_fn=fetch_fn,
            context=context,
            tier_position=tier_pos,
            tier_total=tier_total,
        )
        started_at = get_clock().perf_counter()
        try:
            result = fetch_fn(provider)
        except NotSupportedError as exc:
            self._handle_not_supported(
                exc=exc,
                context=context,
                provider=provider.name,
                latency_ms=self._elapsed_ms(started_at),
                tier_pos=tier_pos,
                tier_total=tier_total,
            )
            reason = "not supported"
            if exc.http_status is not None:
                reason += f" (HTTP {exc.http_status})"
            return _ProviderAttempt(unsupported_reason=reason)
        except FinanceError as exc:
            self._handle_provider_failure(
                exc=exc,
                state=state,
                context=context,
                provider=provider.name,
                latency_ms=self._elapsed_ms(started_at),
                tier_pos=tier_pos,
                tier_total=tier_total,
                is_fallback=is_fallback,
            )
            return _ProviderAttempt(failure=exc)
        except Exception as exc:  # noqa: BLE001 — provider bugs must not break tier walk
            failure = self._unexpected_provider_error(provider, context.endpoint, exc)
            self._handle_provider_failure(
                exc=failure,
                state=state,
                context=context,
                provider=provider.name,
                latency_ms=self._elapsed_ms(started_at),
                tier_pos=tier_pos,
                tier_total=tier_total,
                is_fallback=is_fallback,
            )
            return _ProviderAttempt(failure=failure)

        if state:
            state.mark_success()
        if self._cache is not None:
            self._cache.mark_endpoint_ok(provider.name, context.endpoint)
        self._audit.record_success(
            context=context,
            provider=provider.name,
            latency_ms=self._elapsed_ms(started_at),
            tier_position=tier_pos,
            tier_total=tier_total,
            is_fallback=is_fallback,
        )
        result = self._augmenter.apply(
            result=result,
            fields=augment_fields,
            remaining_providers=remaining_providers,
            fetch_fn=fetch_fn,
            context=context,
            tier_total=tier_total,
            prefetch=prefetch,
        )
        return _ProviderAttempt(result=result)

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return (get_clock().perf_counter() - started_at) * 1000

    @staticmethod
    def _unexpected_provider_error(
        provider: BaseProvider,
        endpoint: str,
        exc: Exception,
    ) -> ProviderError:
        """Normalize provider bugs so callers never receive raw exceptions."""
        logger.warning(
            "Provider %s raised unexpected %s for %s: %s",
            provider.name,
            type(exc).__name__,
            endpoint,
            exc,
        )
        return ProviderError(
            code="PROVIDER_UNEXPECTED_ERROR",
            message=f"{type(exc).__name__}: {exc}",
            provider=provider.name,
            retry_safe=False,
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
                self._cache.set_router_state(provider, context.endpoint, state.to_persisted_dict())
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

        # P4: plan-gated 402/403 apply to all symbols — unless this
        # (provider, endpoint) succeeded recently, in which case the 403 is
        # symbol-gated (e.g. Finnhub free tier 403s international listings
        # while US symbols work) and only the per-symbol entry is written.
        # Without the veto, one gated symbol benched the whole endpoint for
        # 24h and pushed every request to slower fallback providers.
        if exc.http_status in {402, 403} and not self._cache.get_endpoint_ok(
            provider, context.endpoint
        ):
            self._cache.set_negative_global(provider, context.endpoint)
        self._cache.set_negative(provider, context.endpoint, context.symbol)

    # -------------------------------------------------------------------
    # State inspection
    # -------------------------------------------------------------------

    def state(self) -> dict[str, dict[str, Any]]:
        """Return the current state of all providers.

        Useful for diagnostics and the ``providers status`` CLI command.
        """
        result: dict[str, dict[str, Any]] = {}
        for name in self._providers:
            endpoints = {
                endpoint: state.to_dict()
                for (provider, endpoint), state in self._state.items()
                if provider == name
            }
            active = list(endpoints.values())
            result[name] = {
                "name": name,
                "available": all(item["available"] for item in active),
                "cooldown_remaining_s": max(
                    (float(item["cooldown_remaining_s"]) for item in active), default=0.0
                ),
                "last_error": next(
                    (item["last_error"] for item in active if item["last_error"]), None
                ),
                "consecutive_failures": max(
                    (int(item["consecutive_failures"]) for item in active), default=0
                ),
                "endpoints": endpoints,
            }
        return result

    def get_provider_state(self, name: str, endpoint: str) -> ProviderState | None:
        """Return the state for one provider endpoint."""
        if name not in self._providers:
            return None
        return self._state_for(name, endpoint)

    def reset_cooldowns(self) -> None:
        """Clear all provider cooldowns (useful for testing)."""
        for s in self._state.values():
            s.cooldown_until = 0.0
            s.last_error = None
            s.consecutive_failures = 0

    def _state_for(self, provider: str, endpoint: str) -> ProviderState:
        key = (provider, endpoint)
        state = self._state.get(key)
        if state is not None:
            return state
        state = ProviderState(name=provider)
        if self._cache is not None:
            persisted = self._cache.get_router_state(provider, endpoint)
            if persisted:
                state.restore_persisted(persisted)
        self._state[key] = state
        return state

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
        resolved = [
            self._providers[n]
            for n in tier_list
            if n in self._providers and self._providers[n].supports(endpoint)
        ]

        if not resolved:
            if tier_list:
                logger.warning(
                    "None of the tier-listed providers for %s are registered: %s",
                    endpoint,
                    tier_list,
                )
            else:
                logger.debug("No tier config for %s, using all providers", endpoint)
            resolved = [
                provider for provider in self._providers.values() if provider.supports(endpoint)
            ]

        # Append fallback providers not already in the resolved list.
        # This ensures e.g. yfinance is always tried last even for endpoints
        # whose tier list doesn't include it.
        already_in_list = {p.name for p in resolved}
        for name in self._config.fallback_order:
            if name not in already_in_list:
                prov = self._providers.get(name)
                if prov is not None and prov.supports(endpoint):
                    resolved.append(prov)
                    already_in_list.add(name)

        return resolved
