"""Cache-aware execution engine used by the public client facade."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any, TypeVar, cast

from onefinance.audit._recorder import AuditContext, AuditRecorder
from onefinance.cache.keys import make_key
from onefinance.cache.manager import CacheManager, default_ttl
from onefinance.core.config import StaleConfig
from onefinance.core.errors import AllProvidersFailedError, FinanceError
from onefinance.core.router import ProviderRouter
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

T = TypeVar("T")
B = TypeVar("B")


@dataclass(frozen=True)
class _BatchCacheLookup:
    results: dict[str, Any]
    missing_symbols: list[str]


class _FetchLockPool:
    """Reference-counted per-key locks for coalescing concurrent cache misses."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._locks: dict[str, tuple[Lock, int]] = {}

    @contextmanager
    def acquire(self, key: str) -> Iterator[None]:
        with self._guard:
            lock, users = self._locks.get(key, (Lock(), 0))
            self._locks[key] = (lock, users + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                current_lock, users = self._locks[key]
                if users == 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (current_lock, users - 1)


class CachedDispatcher:
    """Coordinate cache lookup, provider routing, storage, and stale fallback."""

    def __init__(
        self,
        *,
        cache: CacheManager,
        router: ProviderRouter,
        audit: AuditRecorder,
        stale: StaleConfig,
        ttl_overrides: dict[str, int],
    ) -> None:
        self._cache = cache
        self._router = router
        self._audit = audit
        self._stale = stale
        self._ttl_overrides = ttl_overrides
        self._locks = _FetchLockPool()

    @property
    def locks(self) -> _FetchLockPool:
        """Expose lock state for diagnostics and compatibility tests."""
        return self._locks

    def fetch(
        self,
        *,
        cache_key: str,
        endpoint: str,
        no_cache: bool,
        provider_name: str | None,
        fetch_fn: Callable[[BaseProvider], T],
        ttl: int | None = None,
        fresh: bool = False,
        symbol: str | None = None,
        secondary_get: Callable[[], T | None] | None = None,
        miss_resolver: Callable[[], T | None] | None = None,
        on_store: Callable[[T], None] | None = None,
        lkg_key: str | None = None,
    ) -> T:
        """Return a cached value or route, store, and return a fresh value."""
        context = AuditContext.new(endpoint, symbol=symbol, cache_key=cache_key)
        stable_lkg_key = lkg_key or cache_key
        effective_ttl = (
            ttl
            if ttl is not None
            else default_ttl(endpoint, fresh=fresh, overrides=self._ttl_overrides)
        )

        if not no_cache:
            cached = self._cache_hit(
                cache_key=cache_key,
                context=context,
                secondary_get=secondary_get,
            )
            if cached is not None:
                return cached

            with self._locks.acquire(cache_key):
                cached = self._cache_hit(
                    cache_key=cache_key,
                    context=context,
                    secondary_get=secondary_get,
                )
                if cached is not None:
                    return cached
                if miss_resolver is not None:
                    resolved = miss_resolver()
                    if resolved is not None:
                        return resolved
                return self._fetch_and_store(
                    cache_key=cache_key,
                    endpoint=endpoint,
                    ttl=effective_ttl,
                    provider_name=provider_name,
                    fetch_fn=fetch_fn,
                    fresh=fresh,
                    context=context,
                    on_store=on_store,
                    lkg_key=stable_lkg_key,
                )

        return self._fetch_and_store(
            cache_key=cache_key,
            endpoint=endpoint,
            ttl=effective_ttl,
            provider_name=provider_name,
            fetch_fn=fetch_fn,
            fresh=fresh,
            context=context,
            on_store=on_store,
            lkg_key=stable_lkg_key,
        )

    def fetch_batch(
        self,
        *,
        symbols: list[str],
        endpoint: str,
        data_type: str,
        ttl: int,
        no_cache: bool,
        provider_name: str | None,
        fetch_fn: Callable[[BaseProvider, list[str]], list[B]],
    ) -> list[B | FinanceError]:
        """Resolve per-symbol cache hits, fetch misses once, and preserve order."""
        context = AuditContext.new(endpoint)
        lookup = self._batch_cache_lookup(
            symbols=symbols,
            data_type=data_type,
            no_cache=no_cache,
            context=context,
        )
        results = cast("dict[str, B | FinanceError]", lookup.results)
        if not lookup.missing_symbols:
            return [results[symbol] for symbol in symbols]

        if no_cache:
            self._fetch_batch_misses(
                results=results,
                missing_symbols=lookup.missing_symbols,
                endpoint=endpoint,
                data_type=data_type,
                ttl=ttl,
                provider_name=provider_name,
                fetch_fn=fetch_fn,
                context=context,
            )
        else:
            self._fetch_coalesced_batch_misses(
                results=results,
                missing_symbols=lookup.missing_symbols,
                endpoint=endpoint,
                data_type=data_type,
                ttl=ttl,
                provider_name=provider_name,
                fetch_fn=fetch_fn,
                context=context,
            )
        return [results[symbol] for symbol in symbols]

    def _fetch_coalesced_batch_misses(
        self,
        *,
        results: dict[str, B | FinanceError],
        missing_symbols: list[str],
        endpoint: str,
        data_type: str,
        ttl: int,
        provider_name: str | None,
        fetch_fn: Callable[[BaseProvider, list[str]], list[B]],
        context: AuditContext,
    ) -> None:
        """Serialize overlapping batch misses and reuse results from earlier callers."""
        lock_keys = sorted(make_key(data_type, symbol=symbol) for symbol in missing_symbols)
        with ExitStack() as locks:
            for lock_key in lock_keys:
                locks.enter_context(self._locks.acquire(lock_key))

            refreshed = self._batch_cache_lookup(
                symbols=missing_symbols,
                data_type=data_type,
                no_cache=False,
                context=context,
            )
            results.update(cast("dict[str, B | FinanceError]", refreshed.results))
            if refreshed.missing_symbols:
                self._fetch_batch_misses(
                    results=results,
                    missing_symbols=refreshed.missing_symbols,
                    endpoint=endpoint,
                    data_type=data_type,
                    ttl=ttl,
                    provider_name=provider_name,
                    fetch_fn=fetch_fn,
                    context=context,
                )

    def _fetch_and_store(
        self,
        *,
        cache_key: str,
        endpoint: str,
        ttl: int,
        provider_name: str | None,
        fetch_fn: Callable[[BaseProvider], T],
        fresh: bool,
        context: AuditContext,
        on_store: Callable[[T], None] | None,
        lkg_key: str,
    ) -> T:
        lkg_ttl = self._stale.ttl_for(endpoint) if self._stale.enabled else None
        try:
            result = self._router.dispatch(
                endpoint,
                fetch_fn,
                fresh=fresh,
                provider_name=provider_name,
                context=context,
            )
        except AllProvidersFailedError:
            stale = self._stale_fallback(
                endpoint=endpoint,
                lkg_key=lkg_key,
                lkg_ttl=lkg_ttl,
                context=context,
            )
            if stale is not None:
                return stale
            raise

        self._cache.set(cache_key, cast(Any, result), ttl=ttl, tag=endpoint)
        if on_store is not None:
            on_store(result)
        if lkg_ttl is not None:
            self._cache.set_last_known_good(
                lkg_key,
                cast(Any, result),
                ttl=lkg_ttl,
                tag=endpoint,
            )
        return result

    def _stale_fallback(
        self,
        *,
        endpoint: str,
        lkg_key: str,
        lkg_ttl: int | None,
        context: AuditContext,
    ) -> T | None:
        if lkg_ttl is None:
            return None
        lkg = self._cache.get_last_known_good(lkg_key)
        if lkg is None:
            return None

        logger.warning("All providers failed for %s; serving stale last-known-good", endpoint)
        self._audit.record_stale_serve(
            context=context,
            stale_age_s=_lkg_age_seconds(lkg),
        )
        return cast(T, lkg)

    def _cache_hit(
        self,
        *,
        cache_key: str,
        context: AuditContext,
        secondary_get: Callable[[], T | None] | None,
    ) -> T | None:
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for %s", cache_key)
            self._audit.record_cache_hit(context=context)
            return cast(T, cached)
        if secondary_get is None:
            return None
        alternative = secondary_get()
        if alternative is None:
            return None
        logger.debug("Cache hit (secondary) for %s", cache_key)
        self._audit.record_cache_hit(context=context)
        return alternative

    def _fetch_batch_misses(
        self,
        *,
        results: dict[str, B | FinanceError],
        missing_symbols: list[str],
        endpoint: str,
        data_type: str,
        ttl: int,
        provider_name: str | None,
        fetch_fn: Callable[[BaseProvider, list[str]], list[B]],
        context: AuditContext,
    ) -> None:
        batch_context = context.derive(symbol=_summarize_symbols(missing_symbols))
        try:
            batch_result = self._router.dispatch(
                endpoint,
                lambda provider: fetch_fn(provider, missing_symbols),
                fresh=False,
                provider_name=provider_name,
                context=batch_context,
            )
            if len(batch_result) != len(missing_symbols):
                logger.warning(
                    "Batch %s mismatch: requested %d, got %d",
                    data_type,
                    len(missing_symbols),
                    len(batch_result),
                )
            self._store_batch_results(
                results=results,
                missing_symbols=missing_symbols,
                batch_result=batch_result,
                data_type=data_type,
                ttl=ttl,
            )
        except FinanceError as exc:
            for symbol in missing_symbols:
                results[symbol] = exc

    def _batch_cache_lookup(
        self,
        *,
        symbols: list[str],
        data_type: str,
        no_cache: bool,
        context: AuditContext,
    ) -> _BatchCacheLookup:
        results: dict[str, Any] = {}
        missing_symbols: list[str] = []
        missing_set: set[str] = set()
        for symbol in symbols:
            cache_key = make_key(data_type, symbol=symbol)
            if not no_cache:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    results[symbol] = cached
                    self._audit.record_cache_hit(
                        context=context.derive(symbol=symbol, cache_key=cache_key),
                    )
                    continue
            if symbol not in missing_set:
                missing_symbols.append(symbol)
                missing_set.add(symbol)
        return _BatchCacheLookup(results=results, missing_symbols=missing_symbols)

    def _store_batch_results(
        self,
        *,
        results: dict[str, B | FinanceError],
        missing_symbols: list[str],
        batch_result: list[B],
        data_type: str,
        ttl: int,
    ) -> None:
        exact_count = len(batch_result) == len(missing_symbols)
        positional = dict(zip(missing_symbols, batch_result, strict=False))
        by_symbol: dict[str, B] = {
            str(getattr(item, "symbol")): item
            for item in batch_result
            if getattr(item, "symbol", None) is not None
        }
        for symbol in missing_symbols:
            item = by_symbol.get(symbol)
            if item is None and exact_count:
                item = positional.get(symbol)
            if item is None:
                results[symbol] = FinanceError(
                    "BATCH_RESULT_MISSING",
                    f"No result returned by provider for {symbol}",
                )
                continue
            results[symbol] = item
            self._cache.set(
                make_key(data_type, symbol=symbol),
                cast(Any, item),
                ttl=ttl,
                tag=data_type,
            )


def _summarize_symbols(symbols: list[str], *, limit: int = 5) -> str:
    shown = symbols[:limit]
    suffix = f",+{len(symbols) - limit}" if len(symbols) > limit else ""
    return ",".join(shown) + suffix


def _lkg_age_seconds(value: Any) -> float | None:
    items = value if isinstance(value, list) else [value]
    fetched = [
        fetched_at
        for fetched_at in (getattr(item, "fetched_at", None) for item in items)
        if fetched_at is not None
    ]
    if not fetched:
        return None
    newest = max(fetched)
    age = float((datetime.now(UTC) - newest).total_seconds())
    return round(max(age, 0.0), 1)
