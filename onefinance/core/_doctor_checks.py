"""Focused checks used by the configuration doctor."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path

from onefinance.core._provider_inventory import ProviderInventory
from onefinance.core.config import OneFinanceConfig

DoctorFinding = dict[str, str]
ProviderMetadata = Mapping[str, tuple[str, str]]

_TRUTHY = {"1", "true", "yes"}


class DoctorCheckSuite:
    """Run independent configuration checks into one ordered finding list."""

    def __init__(
        self,
        config: OneFinanceConfig,
        inventory: ProviderInventory,
        provider_metadata: ProviderMetadata,
        *,
        config_path: str | None,
    ) -> None:
        self._config = config
        self._inventory = inventory
        self._provider_metadata = provider_metadata
        self._config_path = config_path
        self._findings: list[DoctorFinding] = []

    def run(self) -> list[DoctorFinding]:
        """Run checks in stable display order."""
        self._check_provider_configuration()
        self._check_tier_references()
        self._check_fallback_order()
        self._check_endpoint_coverage()
        self._check_config_file()
        self._check_cache()
        self._check_environment_overrides()
        return self._findings

    def _add(
        self,
        level: str,
        check: str,
        message: str,
        suggestion: str = "",
    ) -> None:
        self._findings.append(
            {
                "level": level,
                "check": check,
                "message": message,
                "suggestion": suggestion,
            }
        )

    def _check_provider_configuration(self) -> None:
        active = self._inventory.instantiated_names
        for provider, (env_var, signup_url) in self._provider_metadata.items():
            if not os.environ.get(env_var):
                self._add(
                    "warning",
                    f"api_key_{provider}",
                    f"{env_var} is not set — {provider} will be skipped",
                    f"export {env_var}=<your_key>   # sign up at {signup_url}",
                )

        if not active - {"yfinance"}:
            self._add(
                "warning",
                "only_yfinance",
                "No paid API keys configured — only yfinance (unofficial scraper) is active",
                (
                    "Set at least one of FMP_API_KEY, FINNHUB_API_KEY, MASSIVE_API_KEY "
                    "for better data quality and reliability"
                ),
            )

    def _check_tier_references(self) -> None:
        active = self._inventory.instantiated_names
        declared = self._inventory.declared_names
        for endpoint, names in self._inventory.tier_refs.items():
            for name in names:
                if name not in declared:
                    self._add(
                        "warning",
                        f"tier_unknown_{endpoint}_{name}",
                        f"Tier for '{endpoint}' references '{name}' not in config.providers",
                        (
                            f"Remove '{name}' from the '{endpoint}' tier list, "
                            "or add it to config.providers"
                        ),
                    )
                elif name not in active and name != "yfinance":
                    env_var = self._provider_metadata.get(name, ("", ""))[0]
                    hint = (
                        f"export {env_var}=<key>  or remove '{name}' from the '{endpoint}' tier"
                        if env_var
                        else f"remove '{name}' from the '{endpoint}' tier"
                    )
                    self._add(
                        "info",
                        f"tier_skipped_{endpoint}_{name}",
                        (
                            f"Tier for '{endpoint}' lists '{name}' but it has no API key"
                            " — will be silently skipped"
                        ),
                        hint,
                    )

    def _check_fallback_order(self) -> None:
        active = self._inventory.instantiated_names
        for name in self._config.fallback_order:
            if name in active:
                continue
            env_var = self._provider_metadata.get(name, ("", ""))[0]
            hint = f"export {env_var}=<key>" if env_var else f"remove '{name}' from fallback_order"
            self._add(
                "info",
                f"fallback_order_skipped_{name}",
                (
                    f"fallback_order contains '{name}' but it is not configured"
                    " — will be silently skipped"
                ),
                hint,
            )

        if not self._config.fallback_order:
            self._add(
                "warning",
                "fallback_order_empty",
                (
                    "fallback_order is empty — no provider will be tried after "
                    "the tier list is exhausted"
                ),
                (
                    "Set OFCLIENT_FALLBACK_ORDER=yfinance or add "
                    "fallback_order: [yfinance] to config.yaml"
                ),
            )

    def _check_endpoint_coverage(self) -> None:
        active = self._inventory.instantiated_names
        for endpoint, names in self._inventory.tier_refs.items():
            tier_active = [name for name in names if name in active]
            fallback_active = [
                name for name in self._config.fallback_order if name in active and name not in names
            ]
            if tier_active or fallback_active:
                continue
            self._add(
                "error",
                f"no_provider_{endpoint}",
                (
                    f"No configured provider can serve '{endpoint}'"
                    " (tier + fallback_order all missing API keys)"
                ),
                (
                    f"Configure at least one provider for '{endpoint}' "
                    f"(tier: {list(names)}) or add yfinance to fallback_order"
                ),
            )

    def _check_config_file(self) -> None:
        configured_path = self._config_path or os.environ.get("OFCLIENT_CONFIG")
        if not configured_path:
            self._add(
                "info",
                "no_config_file",
                "No config file in use (OFCLIENT_CONFIG not set) — using built-in defaults",
                "Run: ofclient config init   to generate a config.yaml you can customise",
            )
            return

        path = Path(configured_path).expanduser()
        if not path.exists():
            self._add(
                "error",
                "config_file_missing",
                f"OFCLIENT_CONFIG points to '{path}' which does not exist",
                f"Create the file with: ofclient config init --output {path}",
            )
            return

        self._validate_config_file(path)

    def _validate_config_file(self, path: Path) -> None:
        try:
            import yaml  # type: ignore[import-untyped]

            with open(path) as config_file:
                raw = yaml.safe_load(config_file)
            if not isinstance(raw, dict):
                self._add(
                    "error",
                    "config_file_invalid",
                    f"Config file '{path}' is not a valid YAML mapping",
                    "Check the file for syntax errors or regenerate with: ofclient config init",
                )
                return

            known_keys = {field.name for field in fields(OneFinanceConfig)}
            unknown_keys = set(raw) - known_keys
            if unknown_keys:
                self._add(
                    "warning",
                    "config_unknown_keys",
                    f"Config file has unrecognised top-level keys: {sorted(unknown_keys)}",
                    f"Valid keys are: {sorted(known_keys)}",
                )
        except ImportError:
            self._add(
                "warning",
                "yaml_not_installed",
                "PyYAML is not installed — OFCLIENT_CONFIG file cannot be validated",
                "uv add pyyaml   # or: pip install pyyaml",
            )
        except Exception as exc:  # noqa: BLE001
            self._add(
                "error",
                "config_file_parse_error",
                f"Failed to parse config file '{path}': {exc}",
                "Fix the YAML syntax or regenerate with: ofclient config init",
            )

    def _check_cache(self) -> None:
        cache_dir = Path(self._config.cache.dir).expanduser()
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            test_file = cache_dir / ".doctor_write_test"
            test_file.touch()
            test_file.unlink()
        except Exception as exc:  # noqa: BLE001
            self._add(
                "error",
                "cache_not_writable",
                f"Cache directory '{cache_dir}' is not writable: {exc}",
                (
                    "Fix permissions or set a different path: "
                    "cache: {dir: /path/to/cache} in config.yaml"
                ),
            )

        if self._config.cache.size_limit_gb < 0.1:
            self._add(
                "warning",
                "cache_size_tiny",
                (
                    f"Cache size limit is very small "
                    f"({self._config.cache.size_limit_gb:.2f} GB)"
                    " — entries will evict frequently"
                ),
                "Increase cache.size_limit_gb to at least 0.5 in config.yaml",
            )

    def _check_environment_overrides(self) -> None:
        if os.environ.get("OFCLIENT_NO_CACHE", "").lower() in _TRUTHY:
            self._add(
                "info",
                "no_cache_active",
                "OFCLIENT_NO_CACHE is set — every request bypasses cache and hits the API directly",
                "Unset OFCLIENT_NO_CACHE to re-enable caching",
            )

        if os.environ.get("OFCLIENT_FALLBACK_ORDER") is not None:
            self._add(
                "info",
                "fallback_order_env",
                f"OFCLIENT_FALLBACK_ORDER is active: {self._config.fallback_order}",
                "Unset OFCLIENT_FALLBACK_ORDER to revert to config-file or built-in default",
            )

        if os.environ.get("OFCLIENT_DRY_RUN", "").lower() in _TRUTHY:
            self._add(
                "info",
                "dry_run_active",
                "OFCLIENT_DRY_RUN is set — no real API calls will be made",
                "Unset OFCLIENT_DRY_RUN to make live requests",
            )
