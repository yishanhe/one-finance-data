"""Provider router — tier walking with cooldown logic.

The router consults a config-driven tier table to select which provider
to try first for each endpoint.  When a provider fails or hits a rate
limit, the router marks it in cooldown and tries the next tier.

See design doc §6 (tier strategy), §7 (quota handling).
"""

from __future__ import annotations

import logging
import threading
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
    ProviderError,
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

    def get_endpoint_ok(self, provider: str, endpoint: str) -> bool: ...

    def mark_endpoint_ok(self, provider: str, endpoint: str, ttl: int = ...) -> None: ...

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


@dataclass(slots=True)
class _AugmentPrefetch:
    """In-flight filler call started concurrently with the primary fetch.

    Spawned when the primary provider statically declares (via
    ``KNOWN_MISSING_FIELDS``) that it can never populate one of the augment
    fields, so the serial post-primary filler call is a certainty.  The
    worker writes ``holder["result"]`` and sets ``done``; a result that
    outlives the request still lands in the augment cache.
    """

    provider_name: str
    tier_position: int
    holder: dict[str, Any]
    done: threading.Event
    started_at: float


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

            prefetch = self._maybe_prefetch_augment(
                prov=prov,
                aug_fields=aug_fields,
                remaining_providers=providers[tier_pos + 1 :],
                fetch_fn=fetch_fn,
                context=audit_context,
                tier_pos=tier_pos,
            )

            t0 = get_clock().perf_counter()
            try:
                result = fetch_fn(prov)
                latency = (get_clock().perf_counter() - t0) * 1000
                if state:
                    state.mark_success()
                if self._cache is not None:
                    self._cache.mark_endpoint_ok(prov.name, audit_context.endpoint)
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
                            prefetch=prefetch,
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

            except Exception as exc:  # noqa: BLE001 — provider bugs must not break the tier walk
                # A provider that crashes outside the FinanceError hierarchy
                # (malformed payload failing model validation, parser bug, …)
                # is a provider failure, not a caller error: wrap it so the
                # router falls through to the next tier and callers always
                # see FinanceError subclasses — never a raw traceback.
                latency = (get_clock().perf_counter() - t0) * 1000
                wrapped = ProviderError(
                    code="PROVIDER_UNEXPECTED_ERROR",
                    message=f"{type(exc).__name__}: {exc}",
                    provider=prov.name,
                    retry_safe=False,
                )
                logger.warning(
                    "Provider %s raised unexpected %s for %s: %s",
                    prov.name,
                    type(exc).__name__,
                    endpoint,
                    exc,
                )
                self._handle_provider_failure(
                    exc=wrapped,
                    state=state,
                    context=audit_context,
                    provider=prov.name,
                    latency_ms=latency,
                    tier_pos=tier_pos,
                    tier_total=tier_total,
                    is_fallback=len(failures) > 0,
                )
                failures.append((prov.name, wrapped))
                continue

        self._audit.record_all_failed(
            context=audit_context,
            tier_total=tier_total,
            error_message=(
                f"all {tier_total} providers skipped or failed ({len(failures)} real failures)"
            ),
        )
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

    def _maybe_prefetch_augment(
        self,
        *,
        prov: BaseProvider,
        aug_fields: list[str],
        remaining_providers: list[BaseProvider],
        fetch_fn: Callable[[BaseProvider], T],
        context: AuditContext,
        tier_pos: int,
    ) -> _AugmentPrefetch | None:
        """Start the augment filler concurrently with the primary call.

        Only fires when the primary provider statically declares (via
        ``KNOWN_MISSING_FIELDS``) that it can never populate one of the
        configured augment fields — then the post-primary filler call is a
        certainty, and running it in parallel hides its latency behind the
        primary request instead of adding it serially after.

        Picks the first remaining provider that is not in cooldown or
        negative-cached.  Skipped when the augment cache already has a
        recent filler for this symbol (the serial path will hit it for
        free).  The worker mirrors the serial augment worker: it writes the
        augment cache on success so even an unconsumed prefetch helps the
        next request.
        """
        if not aug_fields:
            return None
        known_missing = prov.KNOWN_MISSING_FIELDS.get(context.endpoint)
        if not known_missing or not any(f in known_missing for f in aug_fields):
            return None
        if (
            context.symbol
            and self._cache is not None
            and self._cache.get_augment(context.endpoint, context.symbol) is not None
        ):
            return None

        for idx, filler in enumerate(remaining_providers):
            state = self._state.get(filler.name)
            if state and not state.is_available:
                continue
            if self._cache is not None and (
                self._cache.get_negative_global(filler.name, context.endpoint)
                or self._cache.get_negative(filler.name, context.endpoint, context.symbol)
            ):
                continue

            holder: dict[str, Any] = {}
            done = threading.Event()

            def _worker(
                filler: BaseProvider = filler,
                holder: dict[str, Any] = holder,
                done: threading.Event = done,
            ) -> None:
                try:
                    aug_result = fetch_fn(filler)
                except Exception:
                    logger.debug(
                        "Augment prefetch provider %s failed for %s",
                        filler.name,
                        context.endpoint,
                        exc_info=True,
                    )
                    done.set()
                    return
                holder["result"] = aug_result
                sym = getattr(aug_result, "symbol", None)
                if sym and self._cache is not None:
                    try:
                        self._cache.set_augment(context.endpoint, sym, aug_result)
                    except Exception:
                        logger.debug("Augment cache write failed", exc_info=True)
                done.set()

            t0 = get_clock().perf_counter()
            threading.Thread(
                target=_worker,
                name=f"augment-prefetch-{filler.name}",
                daemon=True,
            ).start()
            logger.debug(
                "Prefetching augment from %s for %s/%s (primary %s misses %s)",
                filler.name,
                context.endpoint,
                context.symbol,
                prov.name,
                sorted(known_missing),
            )
            return _AugmentPrefetch(
                provider_name=filler.name,
                tier_position=tier_pos + 1 + idx,
                holder=holder,
                done=done,
                started_at=t0,
            )
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
        prefetch: _AugmentPrefetch | None = None,
    ) -> T:
        """Best-effort: fill missing fields in *result* from remaining providers.

        Skips providers in cooldown.  Swallows all exceptions — augment is
        opportunistic and must never block the primary result from returning.
        Does not update cooldown state for augment-specific failures.

        Filler calls run under a total wall-clock budget
        (``config.augment.timeout_s``). Each call runs in a daemon thread;
        if the budget expires the primary result is returned as-is, while
        the in-flight call finishes in the background and writes its result
        to the augment cache for the next request. Daemon threads (not a
        ``ThreadPoolExecutor``) so an abandoned slow call cannot block
        interpreter shutdown.

        When *prefetch* is given (a filler call started concurrently with
        the primary fetch, see ``_maybe_prefetch_augment``), it is consumed
        first and its provider is excluded from the serial loop.
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

        budget_s = self._config.augment.timeout_s
        deadline = get_clock().perf_counter() + budget_s

        # Consume a concurrent prefetch first — it started alongside the
        # primary call, so most (often all) of its latency is already hidden.
        if prefetch is not None:
            if prefetch.done.wait(timeout=budget_s):
                aug_result = prefetch.holder.get("result")
                if aug_result is not None:
                    latency = (get_clock().perf_counter() - prefetch.started_at) * 1000
                    merged = _merge_model(current, aug_result, all_aug_fields)
                    if merged is not current:
                        self._audit.record_augment(
                            context=context,
                            provider=prefetch.provider_name,
                            latency_ms=latency,
                            tier_position=prefetch.tier_position,
                            tier_total=tier_total,
                        )
                        current = merged
                        still_missing = [
                            f for f in all_aug_fields if _is_missing(getattr(current, f, None))
                        ]
            else:
                self._audit.record_skipped(
                    context=context,
                    provider=prefetch.provider_name,
                    tier_position=prefetch.tier_position,
                    tier_total=tier_total,
                    reason=f"augment timeout ({budget_s:.1f}s budget, prefetched)",
                )
                # Budget spent waiting on the prefetch — no time left for
                # serial fillers either.
                return current

        for aug_idx, prov in enumerate(remaining_providers):
            if not still_missing:
                break
            if prefetch is not None and prov.name == prefetch.provider_name:
                continue  # already ran concurrently — never call it twice
            state = self._state.get(prov.name)
            if state and not state.is_available:
                continue
            remaining_s = deadline - get_clock().perf_counter()
            aug_tier_pos = tier_total - len(remaining_providers) + aug_idx
            if remaining_s <= 0:
                self._audit.record_skipped(
                    context=context,
                    provider=prov.name,
                    tier_position=aug_tier_pos,
                    tier_total=tier_total,
                    reason=f"augment budget exhausted ({budget_s:.1f}s)",
                )
                break

            holder: dict[str, Any] = {}
            done = threading.Event()

            def _worker(
                prov: BaseProvider = prov,
                holder: dict[str, Any] = holder,
                done: threading.Event = done,
            ) -> None:
                try:
                    aug_result = fetch_fn(prov)
                except Exception:
                    logger.debug(
                        "Augment provider %s skipped for %s",
                        prov.name,
                        context.endpoint,
                        exc_info=True,
                    )
                    done.set()
                    return
                holder["result"] = aug_result
                # P2-A: cache the raw augment result for ~5 min. Written from
                # the worker so a call that outlives the budget still lands in
                # the augment cache for the next request.
                if sym and self._cache is not None:
                    try:
                        self._cache.set_augment(context.endpoint, sym, aug_result)
                    except Exception:
                        logger.debug("Augment cache write failed", exc_info=True)
                done.set()

            t0 = get_clock().perf_counter()
            thread = threading.Thread(
                target=_worker,
                name=f"augment-{prov.name}",
                daemon=True,
            )
            thread.start()
            if not done.wait(timeout=remaining_s):
                self._audit.record_skipped(
                    context=context,
                    provider=prov.name,
                    tier_position=aug_tier_pos,
                    tier_total=tier_total,
                    reason=f"augment timeout ({budget_s:.1f}s budget)",
                )
                logger.debug(
                    "Augment provider %s timed out for %s/%s (budget %.1fs)",
                    prov.name,
                    context.endpoint,
                    sym,
                    budget_s,
                )
                break

            aug_result = holder.get("result")
            if aug_result is None:
                continue
            latency = (get_clock().perf_counter() - t0) * 1000
            merged = _merge_model(current, aug_result, all_aug_fields)
            if merged is not current:
                self._audit.record_augment(
                    context=context,
                    provider=prov.name,
                    latency_ms=latency,
                    tier_position=aug_tier_pos,
                    tier_total=tier_total,
                )
                current = merged
                still_missing = [
                    f for f in all_aug_fields if _is_missing(getattr(current, f, None))
                ]

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
