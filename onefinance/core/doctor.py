"""Config self-check for ``ofclient doctor``.

Runs a suite of offline checks against the resolved config and environment,
returns a structured list of findings with severity levels and actionable
suggestions.  Never raises — every failure is captured as a finding.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from onefinance.core.config import OneFinanceConfig

# Provider → (env var, signup URL)
_PROVIDER_META: dict[str, tuple[str, str]] = {
    "fmp": ("FMP_API_KEY", "https://financialmodelingprep.com/developer/docs"),
    "finnhub": ("FINNHUB_API_KEY", "https://finnhub.io"),
    "twelve_data": ("TWELVE_DATA_API_KEY", "https://twelvedata.com"),
    "alpha_vantage": ("ALPHAVANTAGE_API_KEY", "https://www.alphavantage.co"),
    "polygon": ("POLYGON_API_KEY", "https://polygon.io"),
}

_LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}


def run_doctor(
    config: OneFinanceConfig,
    provider_map: dict[str, Any],
    *,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Run all checks and return a structured report.

    Parameters
    ----------
    config:
        Resolved ``OneFinanceConfig``.
    provider_map:
        Already-instantiated providers from ``OneFinanceClient._provider_map``.
    config_path:
        Path to the YAML config file (if any) — used for file existence checks.
    """
    findings: list[dict[str, str]] = []

    def _add(level: str, check: str, message: str, suggestion: str = "") -> None:
        findings.append(
            {
                "level": level,
                "check": check,
                "message": message,
                "suggestion": suggestion,
            }
        )

    # ------------------------------------------------------------------
    # 1. API key checks — per provider
    # ------------------------------------------------------------------
    configured_providers = set(provider_map.keys())

    for prov_name, (env_var, signup_url) in _PROVIDER_META.items():
        key_present = bool(os.environ.get(env_var))
        if not key_present:
            _add(
                "warning",
                f"api_key_{prov_name}",
                f"{env_var} is not set — {prov_name} will be skipped",
                f"export {env_var}=<your_key>   # sign up at {signup_url}",
            )

    # ------------------------------------------------------------------
    # 2. Only yfinance available
    # ------------------------------------------------------------------
    paid_providers = configured_providers - {"yfinance"}
    if not paid_providers:
        _add(
            "warning",
            "only_yfinance",
            "No paid API keys configured — only yfinance (unofficial scraper) is active",
            (
                "Set at least one of FMP_API_KEY, FINNHUB_API_KEY, POLYGON_API_KEY "
                "for better data quality and reliability"
            ),
        )

    # ------------------------------------------------------------------
    # 3. Tier list references to unconfigured providers
    # ------------------------------------------------------------------
    tier_refs = config.flat_tier_refs()
    known_providers = set(config.providers.keys())

    for endpoint, names in tier_refs.items():
        for name in names:
            if name not in known_providers:
                _add(
                    "warning",
                    f"tier_unknown_{endpoint}_{name}",
                    f"Tier for '{endpoint}' references '{name}' not in config.providers",
                    (
                        f"Remove '{name}' from the '{endpoint}' tier list, "
                        "or add it to config.providers"
                    ),
                )
            elif name not in configured_providers and name != "yfinance":
                env_var = _PROVIDER_META.get(name, ("", ""))[0]
                if env_var:
                    hint = f"export {env_var}=<key>  or remove '{name}' from the '{endpoint}' tier"
                else:
                    hint = f"remove '{name}' from the '{endpoint}' tier"
                _add(
                    "info",
                    f"tier_skipped_{endpoint}_{name}",
                    (
                        f"Tier for '{endpoint}' lists '{name}' but it has no API key"
                        " — will be silently skipped"
                    ),
                    hint,
                )

    # ------------------------------------------------------------------
    # 4. fallback_order references to unconfigured providers
    # ------------------------------------------------------------------
    for name in config.fallback_order:
        if name not in configured_providers:
            env_var = _PROVIDER_META.get(name, ("", ""))[0]
            hint = f"export {env_var}=<key>" if env_var else f"remove '{name}' from fallback_order"
            _add(
                "info",
                f"fallback_order_skipped_{name}",
                (
                    f"fallback_order contains '{name}' but it is not configured"
                    " — will be silently skipped"
                ),
                hint,
            )

    if not config.fallback_order:
        _add(
            "warning",
            "fallback_order_empty",
            "fallback_order is empty — no provider will be tried after the tier list is exhausted",
            "Set OFCLIENT_FALLBACK_ORDER=yfinance or add fallback_order: [yfinance] to config.yaml",
        )

    # ------------------------------------------------------------------
    # 5. Endpoints with zero active providers (tier + fallback)
    # ------------------------------------------------------------------
    active_set = configured_providers
    for endpoint, names in tier_refs.items():
        tier_active = [n for n in names if n in active_set]
        fallback_active = [n for n in config.fallback_order if n in active_set and n not in names]
        if not tier_active and not fallback_active:
            _add(
                "error",
                f"no_provider_{endpoint}",
                (
                    f"No configured provider can serve '{endpoint}'"
                    " (tier + fallback_order all missing API keys)"
                ),
                (
                    f"Configure at least one provider for '{endpoint}' "
                    f"(tier: {names}) or add yfinance to fallback_order"
                ),
            )

    # ------------------------------------------------------------------
    # 6. OFCLIENT_CONFIG file checks
    # ------------------------------------------------------------------
    config_env = config_path or os.environ.get("OFCLIENT_CONFIG")
    if config_env:
        cfg_path = Path(config_env).expanduser()
        if not cfg_path.exists():
            _add(
                "error",
                "config_file_missing",
                f"OFCLIENT_CONFIG points to '{cfg_path}' which does not exist",
                f"Create the file with: ofclient config init --output {cfg_path}",
            )
        else:
            try:
                import yaml  # type: ignore[import-untyped]

                with open(cfg_path) as f:
                    raw = yaml.safe_load(f)
                if not isinstance(raw, dict):
                    _add(
                        "error",
                        "config_file_invalid",
                        f"Config file '{cfg_path}' is not a valid YAML mapping",
                        "Check the file for syntax errors or regenerate with: ofclient config init",
                    )
                else:
                    known_keys = {
                        "providers",
                        "tiers",
                        "cache",
                        "cooldown",
                        "augment",
                        "fallback_order",
                    }
                    unknown_keys = set(raw.keys()) - known_keys
                    if unknown_keys:
                        _add(
                            "warning",
                            "config_unknown_keys",
                            f"Config file has unrecognised top-level keys: {sorted(unknown_keys)}",
                            f"Valid keys are: {sorted(known_keys)}",
                        )
            except ImportError:
                _add(
                    "warning",
                    "yaml_not_installed",
                    "PyYAML is not installed — OFCLIENT_CONFIG file cannot be validated",
                    "uv add pyyaml   # or: pip install pyyaml",
                )
            except Exception as exc:  # noqa: BLE001
                _add(
                    "error",
                    "config_file_parse_error",
                    f"Failed to parse config file '{cfg_path}': {exc}",
                    "Fix the YAML syntax or regenerate with: ofclient config init",
                )
    else:
        _add(
            "info",
            "no_config_file",
            "No config file in use (OFCLIENT_CONFIG not set) — using built-in defaults",
            "Run: ofclient config init   to generate a config.yaml you can customise",
        )

    # ------------------------------------------------------------------
    # 7. Cache directory
    # ------------------------------------------------------------------
    cache_dir = Path(config.cache.dir).expanduser()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        test_file = cache_dir / ".doctor_write_test"
        test_file.touch()
        test_file.unlink()
    except Exception as exc:  # noqa: BLE001
        _add(
            "error",
            "cache_not_writable",
            f"Cache directory '{cache_dir}' is not writable: {exc}",
            "Fix permissions or set a different path: cache: {dir: /path/to/cache} in config.yaml",
        )

    if config.cache.size_limit_gb < 0.1:
        _add(
            "warning",
            "cache_size_tiny",
            (
                f"Cache size limit is very small ({config.cache.size_limit_gb:.2f} GB)"
                " — entries will evict frequently"
            ),
            "Increase cache.size_limit_gb to at least 0.5 in config.yaml",
        )

    # ------------------------------------------------------------------
    # 8. Active environment variables
    # ------------------------------------------------------------------
    if os.environ.get("OFCLIENT_NO_CACHE", "").lower() in ("1", "true", "yes"):
        _add(
            "info",
            "no_cache_active",
            "OFCLIENT_NO_CACHE is set — every request bypasses cache and hits the API directly",
            "Unset OFCLIENT_NO_CACHE to re-enable caching",
        )

    fallback_env = os.environ.get("OFCLIENT_FALLBACK_ORDER")
    if fallback_env is not None:
        _add(
            "info",
            "fallback_order_env",
            f"OFCLIENT_FALLBACK_ORDER is active: {config.fallback_order}",
            "Unset OFCLIENT_FALLBACK_ORDER to revert to config-file or built-in default",
        )

    if os.environ.get("OFCLIENT_DRY_RUN", "").lower() in ("1", "true", "yes"):
        _add(
            "info",
            "dry_run_active",
            "OFCLIENT_DRY_RUN is set — no real API calls will be made",
            "Unset OFCLIENT_DRY_RUN to make live requests",
        )

    # ------------------------------------------------------------------
    # Build summary
    # ------------------------------------------------------------------
    counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    for finding in findings:
        counts[finding["level"]] = counts.get(finding["level"], 0) + 1

    healthy = counts["error"] == 0

    return {
        "healthy": healthy,
        "summary": {
            "errors": counts["error"],
            "warnings": counts["warning"],
            "info": counts["info"],
        },
        "active_providers": sorted(configured_providers),
        "fallback_order": config.fallback_order,
        "findings": sorted(findings, key=lambda item: _LEVEL_ORDER.get(item["level"], 99)),
    }
