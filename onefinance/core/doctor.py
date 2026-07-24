"""Config self-check for ``ofclient doctor``."""

from __future__ import annotations

from typing import Any

from onefinance.core._doctor_checks import DoctorCheckSuite
from onefinance.core._provider_inventory import ProviderInventory
from onefinance.core.config import OneFinanceConfig

# Provider → (env var, signup URL)
_PROVIDER_META: dict[str, tuple[str, str]] = {
    "fmp": ("FMP_API_KEY", "https://financialmodelingprep.com/developer/docs"),
    "finnhub": ("FINNHUB_API_KEY", "https://finnhub.io"),
    "twelve_data": ("TWELVE_DATA_API_KEY", "https://twelvedata.com"),
    "alpha_vantage": ("ALPHAVANTAGE_API_KEY", "https://www.alphavantage.co"),
    "massive": ("MASSIVE_API_KEY", "https://massive.com"),
}

_LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}


def run_doctor(
    config: OneFinanceConfig,
    provider_map: dict[str, Any],
    *,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Run offline configuration checks and return a structured report."""
    inventory = ProviderInventory(config, provider_map)
    findings = DoctorCheckSuite(
        config,
        inventory,
        _PROVIDER_META,
        config_path=config_path,
    ).run()

    counts = {
        level: sum(finding["level"] == level for finding in findings) for level in _LEVEL_ORDER
    }
    return {
        "healthy": counts["error"] == 0,
        "summary": {
            "errors": counts["error"],
            "warnings": counts["warning"],
            "info": counts["info"],
        },
        "active_providers": sorted(inventory.instantiated_names),
        "fallback_order": config.fallback_order,
        "findings": sorted(
            findings,
            key=lambda finding: _LEVEL_ORDER.get(finding["level"], 99),
        ),
    }
