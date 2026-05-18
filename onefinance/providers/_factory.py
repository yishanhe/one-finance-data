"""Provider registry + factory.

Replaces hand-rolled ``if/elif`` provider construction with a self-registering
table. Each built-in provider registers a ``ProviderSpec`` here; the
``OneFinanceClient`` walks the registry when no explicit provider list is
passed.

Adding a new provider is a one-file change: drop a module under
``onefinance/providers/`` and append a ``register(...)`` call in
``onefinance/providers/__init__.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from onefinance.core.config import OneFinanceConfig, ProviderConfig
from onefinance.providers.base import BaseProvider

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

Builder = Callable[[ProviderConfig, "httpx.Client | None"], "BaseProvider | None"]


@dataclass(frozen=True)
class ProviderSpec:
    """Static registration record for a provider."""

    name: str
    builder: Builder
    requires_api_key: bool


_REGISTRY: dict[str, ProviderSpec] = {}


def register(spec: ProviderSpec) -> ProviderSpec:
    """Register *spec* under its name. Idempotent — same name overrides."""
    _REGISTRY[spec.name] = spec
    return spec


def iter_specs() -> Iterable[ProviderSpec]:
    """Iterate every registered spec in insertion order."""
    return _REGISTRY.values()


def build(
    name: str,
    cfg: ProviderConfig,
    http_client: httpx.Client | None = None,
) -> BaseProvider | None:
    """Build a single provider by name. Returns ``None`` if absent or skipped."""
    spec = _REGISTRY.get(name)
    if spec is None:
        logger.debug("No provider spec registered for %r", name)
        return None
    try:
        return spec.builder(cfg, http_client)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Provider %r failed to build: %s", name, exc)
        return None


def build_all(
    config: OneFinanceConfig,
    http_client: httpx.Client | None = None,
) -> list[BaseProvider]:
    """Build every provider whose credentials/config are available.

    Iterates ``config.providers`` in declaration order. Skips providers whose
    builder returns ``None`` (typically: missing API key). Falls back to a
    bare ``YFinanceProvider`` when no provider could be built — preserves the
    pre-factory invariant that the client never starts empty-handed.
    """
    providers: list[BaseProvider] = []
    for name, prov_cfg in config.providers.items():
        if not isinstance(prov_cfg, ProviderConfig):
            continue
        instance = build(name, prov_cfg, http_client)
        if instance is not None:
            providers.append(instance)

    if not providers:
        from onefinance.providers.yfinance_provider import YFinanceProvider

        providers = [YFinanceProvider()]
    return providers
