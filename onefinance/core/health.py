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
    gates = plan_gated or []
    report = ProviderHealthCheck(
        inventory,
        provider_map,
        ping=ping,
        ping_symbol=ping_symbol,
        ping_timeout_s=ping_timeout_s,
        only=only,
        plan_gated=gates,
    ).run()
    report["plan_gate_recommendations"] = _plan_gate_recommendations(config, gates)
    return report


def _plan_gate_recommendations(
    config: OneFinanceConfig,
    plan_gated: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Suggest a tier order that avoids endpoints the active plan has benched."""
    gated_by_endpoint: dict[str, set[str]] = {}
    for provider, endpoint in plan_gated:
        gated_by_endpoint.setdefault(endpoint, set()).add(provider)

    recommendations: list[dict[str, Any]] = []
    for endpoint, gated in sorted(gated_by_endpoint.items()):
        tier = config.get_tier_list(endpoint)
        working_first = [provider for provider in tier if provider not in gated]
        if working_first:
            recommendations.append(
                {
                    "endpoint": endpoint,
                    "move_after": sorted(gated),
                    "prefer_first": working_first[0],
                    "suggestion": (
                        f"Move {working_first[0]} ahead of plan-gated providers "
                        f"for {endpoint} in tiers."
                    ),
                }
            )
    return recommendations
