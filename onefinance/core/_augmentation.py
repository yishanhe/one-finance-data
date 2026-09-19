"""Best-effort null-fill enrichment for provider results."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from onefinance._clock import get_clock
from onefinance.audit._recorder import AuditContext, AuditRecorder
from onefinance.core._router_state import ProviderState, RouterCache, cache_not_supported
from onefinance.core.config import AugmentConfig
from onefinance.core.errors import NotSupportedError
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

T = TypeVar("T")


def is_missing(value: Any) -> bool:
    """Return whether a field is absent for null-fill purposes."""
    return value is None or (isinstance(value, (int, float)) and value == 0)


def missing_fields(model: Any, fields: list[str]) -> list[str]:
    """Return configured fields that still need null-fill enrichment."""
    return [field for field in fields if is_missing(getattr(model, field, None))]


def merge_model(base: Any, filler: Any, fields: list[str]) -> Any:
    """Fill missing fields in a frozen model and combine source attribution."""
    updates: dict[str, Any] = {}
    for field in fields:
        if is_missing(getattr(base, field, None)) and not is_missing(getattr(filler, field, None)):
            updates[field] = getattr(filler, field)
    if not updates:
        return base

    base_source: str = getattr(base, "source", "")
    filler_source: str = getattr(filler, "source", "")
    if filler_source and filler_source not in base_source:
        updates["source"] = f"{base_source}+{filler_source}"
    return base.model_copy(update=updates)


@dataclass(slots=True)
class AugmentPrefetch:
    """In-flight filler call started concurrently with the primary fetch."""

    provider_name: str
    tier_position: int
    holder: dict[str, Any]
    done: threading.Event


class ResultAugmenter:
    """Coordinate cached, prefetched, and serial null-fill enrichment."""

    def __init__(
        self,
        config: AugmentConfig,
        cache: RouterCache | None,
        states: Mapping[tuple[str, str], ProviderState],
        audit: AuditRecorder,
    ) -> None:
        self._config = config
        self._cache = cache
        self._states = states
        self._audit = audit

    def fields_for(self, endpoint: str) -> list[str]:
        """Return the fields whose absence triggers enrichment for an endpoint."""
        return list(self._config.fields.get(endpoint, [])) if self._config.enabled else []

    def _merge_fields(self, endpoint: str, fields: list[str]) -> list[str]:
        """Return trigger fields plus merge-only extras.

        Extras ride along on a filler response we already hold — they widen what
        a merge copies without widening what makes us call a filler.
        """
        extras = self._config.extra_fields.get(endpoint, ())
        return fields + [field for field in extras if field not in fields]

    def maybe_prefetch(
        self,
        *,
        primary: BaseProvider,
        fields: list[str],
        remaining_providers: list[BaseProvider],
        fetch_fn: Callable[[BaseProvider], T],
        context: AuditContext,
        tier_position: int,
        tier_total: int,
    ) -> AugmentPrefetch | None:
        """Start a certain filler call alongside a structurally incomplete primary."""
        if not fields:
            return None
        known_missing = primary.KNOWN_MISSING_FIELDS.get(context.endpoint)
        if not known_missing or not any(field in known_missing for field in fields):
            return None
        if (
            context.symbol
            and self._cache is not None
            and self._cache.get_augment(context.endpoint, context.symbol) is not None
        ):
            return None

        needed = [field for field in fields if field in known_missing]
        for offset, filler in enumerate(remaining_providers):
            if not self._provider_available(filler, context, needed):
                continue

            holder, done = self._start_filler(
                provider=filler,
                fetch_fn=fetch_fn,
                context=context,
                tier_position=tier_position + 1 + offset,
                tier_total=tier_total,
                thread_name=f"augment-prefetch-{filler.name}",
            )
            logger.debug(
                "Prefetching augment from %s for %s/%s (primary %s misses %s)",
                filler.name,
                context.endpoint,
                context.symbol,
                primary.name,
                sorted(known_missing),
            )
            return AugmentPrefetch(
                provider_name=filler.name,
                tier_position=tier_position + 1 + offset,
                holder=holder,
                done=done,
            )
        return None

    def apply(
        self,
        *,
        result: T,
        fields: list[str],
        remaining_providers: list[BaseProvider],
        fetch_fn: Callable[[BaseProvider], T],
        context: AuditContext,
        tier_total: int,
        prefetch: AugmentPrefetch | None = None,
    ) -> T:
        """Return a best-effort enriched result without exceeding the time budget."""
        if not fields or not hasattr(result, "model_copy"):
            return result

        missing = missing_fields(result, fields)
        if not missing:
            return result

        merge_fields = self._merge_fields(context.endpoint, fields)
        current = result
        symbol = getattr(result, "symbol", None)
        cached = self._cached_result(context, symbol) if prefetch is None else None
        if cached is not None:
            merged = merge_model(current, cached, merge_fields)
            if merged is not current:
                logger.debug("Augment cache hit for %s/%s", context.endpoint, symbol)
                self._audit.record_augment_cache_hit(context=context)
                current = cast(T, merged)
                missing = missing_fields(current, fields)
                # Extras alone can satisfy the merge without filling a trigger
                # field — only stop here once nothing that warrants a call is left.
                if not missing:
                    return current

        budget_s = self._config.timeout_s
        deadline = get_clock().perf_counter() + budget_s
        current, missing, prefetch_timed_out = self._consume_prefetch(
            current=current,
            fields=fields,
            missing=missing,
            prefetch=prefetch,
            context=context,
            tier_total=tier_total,
            budget_s=budget_s,
        )
        if prefetch_timed_out:
            return current

        for offset, provider in enumerate(remaining_providers):
            if not missing:
                break
            if prefetch is not None and provider.name == prefetch.provider_name:
                continue
            if not self._provider_available(provider, context, missing):
                continue

            remaining_s = deadline - get_clock().perf_counter()
            tier_position = tier_total - len(remaining_providers) + offset
            if remaining_s <= 0:
                self._audit.record_skipped(
                    context=context,
                    provider=provider.name,
                    tier_position=tier_position,
                    tier_total=tier_total,
                    reason=f"augment budget exhausted ({budget_s:.1f}s)",
                )
                break

            filler_result, timed_out = self._fetch_with_budget(
                provider=provider,
                fetch_fn=fetch_fn,
                context=context,
                symbol=symbol,
                timeout_s=remaining_s,
                budget_s=budget_s,
                tier_position=tier_position,
                tier_total=tier_total,
            )
            if timed_out:
                break
            if filler_result is None:
                continue
            merged = merge_model(current, filler_result, merge_fields)
            if merged is current:
                continue
            current = cast(T, merged)
            missing = missing_fields(current, fields)

        return current

    def _consume_prefetch(
        self,
        *,
        current: T,
        fields: list[str],
        missing: list[str],
        prefetch: AugmentPrefetch | None,
        context: AuditContext,
        tier_total: int,
        budget_s: float,
    ) -> tuple[T, list[str], bool]:
        if prefetch is None:
            return current, missing, False
        if not prefetch.done.wait(timeout=budget_s):
            self._audit.record_skipped(
                context=context,
                provider=prefetch.provider_name,
                tier_position=prefetch.tier_position,
                tier_total=tier_total,
                reason=f"augment timeout ({budget_s:.1f}s budget, prefetched)",
            )
            return current, missing, True

        filler = prefetch.holder.get("result")
        if filler is None:
            return current, missing, False
        merged = merge_model(current, filler, self._merge_fields(context.endpoint, fields))
        if merged is current:
            return current, missing, False

        enriched = cast(T, merged)
        still_missing = missing_fields(enriched, fields)
        return enriched, still_missing, False

    def _fetch_with_budget(
        self,
        *,
        provider: BaseProvider,
        fetch_fn: Callable[[BaseProvider], T],
        context: AuditContext,
        symbol: str | None,
        timeout_s: float,
        budget_s: float,
        tier_position: int,
        tier_total: int,
    ) -> tuple[T | None, bool]:
        holder, done = self._start_filler(
            provider=provider,
            fetch_fn=fetch_fn,
            context=context,
            tier_position=tier_position,
            tier_total=tier_total,
            thread_name=f"augment-{provider.name}",
        )
        if not done.wait(timeout=timeout_s):
            self._audit.record_skipped(
                context=context,
                provider=provider.name,
                tier_position=tier_position,
                tier_total=tier_total,
                reason=f"augment timeout ({budget_s:.1f}s budget)",
            )
            logger.debug(
                "Augment provider %s timed out for %s/%s (budget %.1fs)",
                provider.name,
                context.endpoint,
                symbol,
                budget_s,
            )
            return None, True

        return cast(T | None, holder.get("result")), False

    def _start_filler(
        self,
        *,
        provider: BaseProvider,
        fetch_fn: Callable[[BaseProvider], T],
        context: AuditContext,
        tier_position: int,
        tier_total: int,
        thread_name: str,
    ) -> tuple[dict[str, Any], threading.Event]:
        """Start a daemon filler and signal completion even when it fails."""
        holder: dict[str, Any] = {}
        done = threading.Event()

        def worker() -> None:
            try:
                result = self._call_filler(provider, fetch_fn, context, tier_position, tier_total)
                if result is not None:
                    holder["result"] = result
            finally:
                done.set()

        threading.Thread(target=worker, name=thread_name, daemon=True).start()
        return holder, done

    def _call_filler(
        self,
        provider: BaseProvider,
        fetch_fn: Callable[[BaseProvider], T],
        context: AuditContext,
        tier_position: int,
        tier_total: int,
    ) -> T | None:
        started = get_clock().perf_counter()
        error: Exception | None = None
        try:
            result = fetch_fn(provider)
            if self._cache is not None:
                self._cache.mark_endpoint_ok(provider.name, context.endpoint)
            self._cache_result(context, result)
            return result
        except Exception as exc:
            error = exc
            if isinstance(exc, NotSupportedError):
                cache_not_supported(
                    self._cache, provider.name, context.endpoint, context.symbol, exc
                )
            logger.debug("Augment provider %s failed", provider.name, exc_info=True)
            return None
        finally:
            self._audit.record_augment(
                context=context,
                provider=provider.name,
                latency_ms=(get_clock().perf_counter() - started) * 1000,
                tier_position=tier_position,
                tier_total=tier_total,
                error=error,
            )

    def _provider_available(
        self, provider: BaseProvider, context: AuditContext, missing: list[str]
    ) -> bool:
        if not provider.supports(context.endpoint):
            return False
        known_missing = provider.KNOWN_MISSING_FIELDS.get(context.endpoint, frozenset())
        if all(field in known_missing for field in missing):
            return False
        state = self._states.get((provider.name, context.endpoint))
        if state and not state.is_available:
            return False
        return self._cache is None or not (
            self._cache.get_negative_global(provider.name, context.endpoint)
            or self._cache.get_negative(provider.name, context.endpoint, context.symbol)
        )

    def _cached_result(self, context: AuditContext, symbol: str | None) -> Any | None:
        if not symbol or self._cache is None:
            return None
        return self._cache.get_augment(context.endpoint, symbol)

    def _cache_result(
        self,
        context: AuditContext,
        result: Any,
        *,
        symbol: str | None = None,
    ) -> None:
        cache_symbol = symbol or getattr(result, "symbol", None)
        if not cache_symbol or self._cache is None:
            return
        try:
            self._cache.set_augment(context.endpoint, cache_symbol, result)
        except Exception:
            logger.debug("Augment cache write failed", exc_info=True)
