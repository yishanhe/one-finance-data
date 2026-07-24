"""Provider configuration and liveness validation."""

from __future__ import annotations

from typing import Any

from onefinance.core._health_checks import ProviderHealthCheck
from onefinance.core._provider_inventory import ProviderInventory
from onefinance.core.config import OneFinanceConfig
from onefinance.providers.base import BaseProvider


def check_providers_health(
    config: OneFinanceConfig,
    provider_map: dict[str, BaseProvider],
    *,
    ping: bool = False,
    ping_symbol: str = "AAPL",
    ping_timeout_s: float = 5.0,
    only: str | None = None,
    plan_gated: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Return config status and optional quote probes for declared providers."""
    inventory = ProviderInventory(config, provider_map)
    return ProviderHealthCheck(
        inventory,
        provider_map,
        ping=ping,
        ping_symbol=ping_symbol,
        ping_timeout_s=ping_timeout_s,
        only=only,
        plan_gated=plan_gated or [],
    ).run()
