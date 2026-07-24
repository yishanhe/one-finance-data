"""Provider health report assembly and optional liveness checks."""

from __future__ import annotations

import time
from typing import Any

from onefinance.core._provider_inventory import ProviderInventory, ProviderRegistration
from onefinance.core.errors import FinanceError
from onefinance.providers.base import BaseProvider


class ProviderHealthCheck:
    """Combine provider inventory with optional live quote probes."""

    def __init__(
        self,
        inventory: ProviderInventory,
        providers: dict[str, BaseProvider],
        *,
        ping: bool,
        ping_symbol: str,
        ping_timeout_s: float,
        only: str | None,
        plan_gated: list[tuple[str, str]],
    ) -> None:
        self._inventory = inventory
        self._providers = providers
        self._ping = ping
        self._ping_symbol = ping_symbol
        self._ping_timeout_s = ping_timeout_s
        self._only = only
        self._plan_gated = plan_gated
        self._gated_by_provider = self._index_plan_gates()

    def run(self) -> dict[str, Any]:
        """Return the complete structured health report."""
        summary = self._initial_summary()
        providers = [
            self._check_provider(registration, summary)
            for registration in self._inventory.registrations(only=self._only)
        ]
        tier_issues = [
            {"endpoint": endpoint, "unknown_provider": provider}
            for endpoint, provider in self._inventory.unknown_tier_references()
        ]
        return {
            "providers": providers,
            "tier_issues": tier_issues,
            "plan_gated": [
                {"provider": provider, "endpoint": endpoint}
                for provider, endpoint in sorted(self._plan_gated)
            ],
            "summary": summary,
        }

    def _check_provider(
        self,
        registration: ProviderRegistration,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        summary["total"] += 1
        status = registration.status
        ping = self._empty_ping()
        if self._ping and registration.instantiated:
            ping = self._probe(self._providers[registration.name])
            summary["pings_succeeded" if ping["ok"] else "pings_failed"] += 1
            if not ping["ok"] and status == "ok":
                status = "ping_failed"
        summary[status] = summary.get(status, 0) + 1

        return {
            "name": registration.name,
            "config": {
                "api_key_env": registration.api_key_env,
                "api_key_present": registration.api_key_present,
                "instantiable": registration.instantiated,
                "in_use_in_tier": registration.in_use_in_tier,
                "tier_endpoints": list(registration.tier_endpoints),
            },
            "plan_gated_endpoints": list(self._gated_by_provider.get(registration.name, ())),
            "ping": ping,
            "status": status,
        }

    def _probe(self, provider: BaseProvider) -> dict[str, Any]:
        result = self._empty_ping()
        result.update(
            {
                "attempted": True,
                "endpoint": "quote",
                "symbol": self._ping_symbol,
            }
        )
        started_at = time.perf_counter()
        try:
            provider.get_quote(self._ping_symbol)
            result["ok"] = True
        except FinanceError as exc:
            result["ok"] = False
            result["error"] = {"code": exc.code, "message": exc.message}
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["error"] = {
                "code": "UNEXPECTED",
                "message": f"{type(exc).__name__}: {exc}",
            }
        result["latency_ms"] = round(
            (time.perf_counter() - started_at) * 1000,
            2,
        )
        return result

    def _initial_summary(self) -> dict[str, Any]:
        return {
            "total": 0,
            "ok": 0,
            "missing_api_key": 0,
            "not_instantiable": 0,
            "unused": 0,
            "ping_failed": 0,
            "pings_succeeded": 0,
            "pings_failed": 0,
            "pings_attempted": self._ping,
            "ping_timeout_s": self._ping_timeout_s if self._ping else None,
        }

    def _index_plan_gates(self) -> dict[str, tuple[str, ...]]:
        endpoints: dict[str, list[str]] = {}
        for provider, endpoint in sorted(self._plan_gated):
            endpoints.setdefault(provider, []).append(endpoint)
        return {
            provider: tuple(provider_endpoints)
            for provider, provider_endpoints in endpoints.items()
        }

    @staticmethod
    def _empty_ping() -> dict[str, Any]:
        return {
            "attempted": False,
            "ok": None,
            "latency_ms": None,
            "endpoint": None,
            "symbol": None,
            "error": None,
        }
