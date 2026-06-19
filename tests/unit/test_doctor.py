"""Unit tests for onefinance.core.doctor — config self-check."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

from onefinance.core.config import CacheConfig, OneFinanceConfig, ProviderConfig
from onefinance.core.doctor import run_doctor
from onefinance.providers.yfinance_provider import YFinanceProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _yfinance_map() -> dict[str, Any]:
    return {"yfinance": YFinanceProvider()}


def _config_only_yfinance(*, fallback_order: list[str] | None = None) -> OneFinanceConfig:
    return OneFinanceConfig(
        providers={"yfinance": ProviderConfig(name="yfinance")},
        tiers={"quote": ["yfinance"]},
        fallback_order=fallback_order if fallback_order is not None else ["yfinance"],
    )


def _config_with_fmp(*, fmp_key_set: bool = False) -> OneFinanceConfig:
    return OneFinanceConfig(
        providers={
            "fmp": ProviderConfig(name="fmp", api_key_env="FMP_API_KEY"),
            "yfinance": ProviderConfig(name="yfinance"),
        },
        tiers={"quote": ["fmp", "yfinance"]},
        fallback_order=["yfinance"],
    )


def _check_names(report: dict[str, Any]) -> set[str]:
    return {f["check"] for f in report["findings"]}


def _findings_by_level(report: dict[str, Any], level: str) -> list[dict[str, Any]]:
    return [f for f in report["findings"] if f["level"] == level]


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


class TestDoctorBaseline:
    def test_returns_required_keys(self) -> None:
        cfg = _config_only_yfinance()
        report = run_doctor(cfg, _yfinance_map())
        assert "healthy" in report
        assert "summary" in report
        assert "findings" in report
        assert "active_providers" in report
        assert "fallback_order" in report

    def test_healthy_flag_false_when_errors(self, tmp_path: Path) -> None:
        cfg = OneFinanceConfig(
            providers={"yfinance": ProviderConfig(name="yfinance")},
            tiers={"dcf": ["fmp"]},  # fmp not in provider_map → no_provider_ error
            fallback_order=[],  # disable fallback so yfinance can't save it
        )
        report = run_doctor(cfg, _yfinance_map())
        errors = _findings_by_level(report, "error")
        assert any("no_provider_dcf" in f["check"] for f in errors)
        assert report["healthy"] is False

    def test_findings_sorted_errors_first(self) -> None:
        cfg = OneFinanceConfig(
            providers={"yfinance": ProviderConfig(name="yfinance")},
            tiers={"dcf": ["fmp"]},
            fallback_order=[],
        )
        report = run_doctor(cfg, _yfinance_map())
        levels = [f["level"] for f in report["findings"]]
        from onefinance.core.doctor import _LEVEL_ORDER

        orders = [_LEVEL_ORDER.get(lv, 99) for lv in levels]
        assert orders == sorted(orders)


# ---------------------------------------------------------------------------
# API key checks
# ---------------------------------------------------------------------------


class TestApiKeyChecks:
    def test_missing_key_generates_warning(self) -> None:
        cfg = _config_with_fmp(fmp_key_set=False)
        with patch.dict("os.environ", {}, clear=True):
            report = run_doctor(cfg, {"yfinance": YFinanceProvider()})
        checks = _check_names(report)
        assert "api_key_fmp" in checks

    def test_key_present_no_warning(self) -> None:
        cfg = _config_with_fmp(fmp_key_set=True)
        with patch.dict("os.environ", {"FMP_API_KEY": "test_key"}):
            report = run_doctor(cfg, {"yfinance": YFinanceProvider()})
        checks = _check_names(report)
        assert "api_key_fmp" not in checks

    def test_only_yfinance_warning_when_no_keys(self) -> None:
        cfg = _config_only_yfinance()
        with patch.dict("os.environ", {}, clear=True):
            report = run_doctor(cfg, _yfinance_map())
        assert "only_yfinance" in _check_names(report)

    def test_no_only_yfinance_warning_when_paid_provider_active(self) -> None:
        from onefinance.providers.fmp import FMPProvider

        with patch.dict("os.environ", {"FMP_API_KEY": "key"}):
            fmp = FMPProvider(api_key="key")
        cfg = _config_with_fmp(fmp_key_set=True)
        with patch.dict("os.environ", {"FMP_API_KEY": "key"}):
            report = run_doctor(cfg, {"fmp": fmp, "yfinance": YFinanceProvider()})
        assert "only_yfinance" not in _check_names(report)


# ---------------------------------------------------------------------------
# Tier list checks
# ---------------------------------------------------------------------------


class TestTierChecks:
    def test_unknown_provider_in_tier_warns(self) -> None:
        cfg = OneFinanceConfig(
            providers={"yfinance": ProviderConfig(name="yfinance")},
            tiers={"quote": ["ghost", "yfinance"]},
            fallback_order=["yfinance"],
        )
        report = run_doctor(cfg, _yfinance_map())
        checks = _check_names(report)
        assert any("tier_unknown_quote_ghost" in c for c in checks)

    def test_configured_but_no_key_info_finding(self) -> None:
        cfg = OneFinanceConfig(
            providers={
                "fmp": ProviderConfig(name="fmp", api_key_env="FMP_API_KEY"),
                "yfinance": ProviderConfig(name="yfinance"),
            },
            tiers={"quote": ["fmp", "yfinance"]},
            fallback_order=["yfinance"],
        )
        with patch.dict("os.environ", {}, clear=True):
            report = run_doctor(cfg, {"yfinance": YFinanceProvider()})
        checks = _check_names(report)
        assert any("tier_skipped_quote_fmp" in c for c in checks)

    def test_no_provider_for_endpoint_is_error(self) -> None:
        cfg = OneFinanceConfig(
            providers={"yfinance": ProviderConfig(name="yfinance")},
            tiers={"dcf": ["fmp"]},  # fmp not available
            fallback_order=[],  # no fallback
        )
        report = run_doctor(cfg, _yfinance_map())
        errors = _findings_by_level(report, "error")
        assert any("no_provider_dcf" in f["check"] for f in errors)

    def test_endpoint_covered_by_fallback_not_error(self) -> None:
        cfg = OneFinanceConfig(
            providers={"yfinance": ProviderConfig(name="yfinance")},
            tiers={"ratios": ["fmp"]},  # fmp not configured
            fallback_order=["yfinance"],  # but yfinance is in fallback
        )
        report = run_doctor(cfg, _yfinance_map())
        errors = _findings_by_level(report, "error")
        assert not any("no_provider_ratios" in f["check"] for f in errors)


# ---------------------------------------------------------------------------
# fallback_order checks
# ---------------------------------------------------------------------------


class TestFallbackOrderChecks:
    def test_empty_fallback_order_warns(self) -> None:
        cfg = _config_only_yfinance(fallback_order=[])
        report = run_doctor(cfg, _yfinance_map())
        assert "fallback_order_empty" in _check_names(report)

    def test_unconfigured_provider_in_fallback_info(self) -> None:
        cfg = OneFinanceConfig(
            providers={"yfinance": ProviderConfig(name="yfinance")},
            tiers={"quote": ["yfinance"]},
            fallback_order=["fmp", "yfinance"],  # fmp not in provider_map
        )
        report = run_doctor(cfg, _yfinance_map())
        checks = _check_names(report)
        assert any("fallback_order_skipped_fmp" in c for c in checks)

    def test_all_fallback_configured_no_warning(self) -> None:
        cfg = _config_only_yfinance(fallback_order=["yfinance"])
        report = run_doctor(cfg, _yfinance_map())
        assert "fallback_order_empty" not in _check_names(report)


# ---------------------------------------------------------------------------
# Config file checks
# ---------------------------------------------------------------------------


class TestConfigFileChecks:
    def test_missing_config_file_is_error(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "nope.yaml")
        cfg = _config_only_yfinance()
        report = run_doctor(cfg, _yfinance_map(), config_path=missing)
        errors = _findings_by_level(report, "error")
        assert any("config_file_missing" in f["check"] for f in errors)
        assert report["healthy"] is False

    def test_valid_config_file_no_error(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("tiers:\n  quote: [yfinance]\n")
        cfg = _config_only_yfinance()
        report = run_doctor(cfg, _yfinance_map(), config_path=str(cfg_file))
        errors = _findings_by_level(report, "error")
        assert not any("config_file" in f["check"] for f in errors)

    def test_unknown_config_keys_warns(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("tiers:\n  quote: [yfinance]\nunknown_key: true\n")
        cfg = _config_only_yfinance()
        report = run_doctor(cfg, _yfinance_map(), config_path=str(cfg_file))
        assert "config_unknown_keys" in _check_names(report)

    def test_no_config_file_yields_info(self) -> None:
        cfg = _config_only_yfinance()
        with patch.dict("os.environ", {}, clear=True):
            report = run_doctor(cfg, _yfinance_map(), config_path=None)
        assert "no_config_file" in _check_names(report)

    def test_invalid_yaml_is_error(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text(":\n  bad: yaml: file:\n    [\n")
        cfg = _config_only_yfinance()
        report = run_doctor(cfg, _yfinance_map(), config_path=str(cfg_file))
        errors = _findings_by_level(report, "error")
        assert any("config_file" in f["check"] for f in errors)


# ---------------------------------------------------------------------------
# Cache checks
# ---------------------------------------------------------------------------


class TestCacheChecks:
    def test_writable_cache_dir_no_error(self, tmp_path: Path) -> None:
        cfg = OneFinanceConfig(
            providers={"yfinance": ProviderConfig(name="yfinance")},
            tiers={"quote": ["yfinance"]},
            cache=CacheConfig(dir=str(tmp_path / "cache")),
            fallback_order=["yfinance"],
        )
        report = run_doctor(cfg, _yfinance_map())
        assert not any("cache" in f["check"] for f in _findings_by_level(report, "error"))

    def test_tiny_cache_limit_warns(self, tmp_path: Path) -> None:
        cfg = OneFinanceConfig(
            providers={"yfinance": ProviderConfig(name="yfinance")},
            tiers={"quote": ["yfinance"]},
            cache=CacheConfig(dir=str(tmp_path / "cache"), size_limit_gb=0.01),
            fallback_order=["yfinance"],
        )
        report = run_doctor(cfg, _yfinance_map())
        assert "cache_size_tiny" in _check_names(report)


# ---------------------------------------------------------------------------
# Env var checks
# ---------------------------------------------------------------------------


class TestEnvVarChecks:
    def test_no_cache_active_info(self) -> None:
        cfg = _config_only_yfinance()
        with patch.dict("os.environ", {"OFCLIENT_NO_CACHE": "1"}):
            report = run_doctor(cfg, _yfinance_map())
        assert "no_cache_active" in _check_names(report)

    def test_dry_run_active_info(self) -> None:
        cfg = _config_only_yfinance()
        with patch.dict("os.environ", {"OFCLIENT_DRY_RUN": "1"}):
            report = run_doctor(cfg, _yfinance_map())
        assert "dry_run_active" in _check_names(report)

    def test_fallback_order_env_info(self) -> None:
        cfg = _config_only_yfinance()
        with patch.dict("os.environ", {"OFCLIENT_FALLBACK_ORDER": "yfinance"}):
            report = run_doctor(cfg, _yfinance_map())
        assert "fallback_order_env" in _check_names(report)

    def test_no_env_vars_no_spurious_info(self) -> None:
        cfg = _config_only_yfinance()
        env_keys = {"OFCLIENT_NO_CACHE", "OFCLIENT_DRY_RUN", "OFCLIENT_FALLBACK_ORDER"}
        with patch.dict("os.environ", {k: "" for k in env_keys}, clear=False):
            # Set to empty string — shouldn't trigger the checks
            for k in env_keys:
                os.environ.pop(k, None)
            report = run_doctor(cfg, _yfinance_map())
        info_checks = {f["check"] for f in _findings_by_level(report, "info")}
        assert "no_cache_active" not in info_checks
        assert "dry_run_active" not in info_checks
        assert "fallback_order_env" not in info_checks
