"""Shared read-only view of configured and instantiated providers."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from onefinance.core.config import OneFinanceConfig


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    """Configuration and runtime availability for one declared provider."""

    name: str
    api_key_env: str | None
    api_key_present: bool
    instantiated: bool
    tier_endpoints: tuple[str, ...]

    @property
    def in_use_in_tier(self) -> bool:
        return bool(self.tier_endpoints)

    @property
    def status(self) -> str:
        if self.api_key_env and not self.api_key_present:
            return "missing_api_key"
        if not self.instantiated:
            return "not_instantiable"
        if not self.in_use_in_tier:
            return "unused"
        return "ok"


class ProviderInventory:
    """Derive provider relationships once for diagnostic consumers."""

    def __init__(
        self,
        config: OneFinanceConfig,
        providers: Mapping[str, object],
    ) -> None:
        self._config = config
        self._declared_names = frozenset(config.providers)
        self._instantiated_names = frozenset(providers)
        self._tier_refs: Mapping[str, tuple[str, ...]] = MappingProxyType(
            {endpoint: tuple(names) for endpoint, names in config.flat_tier_refs().items()}
        )
        self._tier_endpoints_by_provider = self._index_tier_endpoints()

    @property
    def declared_names(self) -> frozenset[str]:
        return self._declared_names

    @property
    def instantiated_names(self) -> frozenset[str]:
        return self._instantiated_names

    @property
    def tier_refs(self) -> Mapping[str, tuple[str, ...]]:
        return self._tier_refs

    def registrations(self, *, only: str | None = None) -> Iterator[ProviderRegistration]:
        """Yield declared providers with derived runtime and tier metadata."""
        for name, provider_config in self._config.providers.items():
            if only is not None and name != only:
                continue
            api_key_env = provider_config.api_key_env
            yield ProviderRegistration(
                name=name,
                api_key_env=api_key_env,
                api_key_present=(
                    True if api_key_env is None else bool(os.environ.get(api_key_env))
                ),
                instantiated=name in self._instantiated_names,
                tier_endpoints=self._tier_endpoints_by_provider.get(name, ()),
            )

    def unknown_tier_references(self) -> Iterator[tuple[str, str]]:
        """Yield ``(endpoint, provider)`` references absent from config."""
        declared = self.declared_names
        for endpoint, names in self._tier_refs.items():
            for name in names:
                if name not in declared:
                    yield endpoint, name

    def _index_tier_endpoints(self) -> dict[str, tuple[str, ...]]:
        endpoints_by_provider: dict[str, list[str]] = {}
        for endpoint, names in self._tier_refs.items():
            for name in names:
                endpoints_by_provider.setdefault(name, []).append(endpoint)
        return {name: tuple(endpoints) for name, endpoints in endpoints_by_provider.items()}
