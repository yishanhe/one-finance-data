"""Tests for onefinance.core.health.check_providers_health()."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from onefinance.core.config import (
    CacheConfig,
    CooldownConfig,
    OneFinanceConfig,
    ProviderConfig,
)
from onefinance.core.errors import RateLimitError
from onefinance.core.health import _flatten_tier_refs, check_providers_health


def _config(
    providers: dict[str, ProviderConfig] | None = None,
    tiers: dict[str, Any] | None = None,
) -> OneFinanceConfig:
    return OneFinanceConfig(
        providers=providers if providers is not None else {
            "fmp": ProviderConfig(name="fmp", api_key_env="FMP_API_KEY"),
            "finnhub": ProviderConfig(name="finnhub", api_key_env="FINNHUB_API_KEY"),
            "yfinance": ProviderConfig(name="yfinance"),
        },
        tiers=tiers if tiers is not None else {
            "price_history": ["fmp", "finnhub", "yfinance"],
            "quote": ["fmp", "yfinance"],
            "ratios": {"default": ["fmp"], "fresh": ["fmp", "finnhub"]},
        },
        cache=CacheConfig(),
        cooldown=CooldownConfig(),
    )


def _fake_provider(name: str, quote_return=None, quote_raises=None) -> MagicMock:
    p = MagicMock()
    p.name = name
    if quote_raises is not None:
        p.get_quote.side_effect = quote_raises
    else:
        p.get_quote.return_value = quote_return or {"symbol": "AAPL", "price": 100.0}
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestFlattenTierRefs:
    def test_flattens_list_form(self):
        out = _flatten_tier_refs({"price_history": ["fmp", "yfinance"]})
        assert out == {"price_history": ["fmp", "yfinance"]}

    def test_flattens_dict_form_with_dedup(self):
        out = _flatten_tier_refs({
            "ratios": {"default": ["fmp", "finnhub"], "fresh": ["fmp"]},
        })
        assert out == {"ratios": ["fmp", "finnhub"]}

    def test_handles_mixed(self):
        out = _flatten_tier_refs({
            "price_history": ["fmp"],
            "ratios": {"default": ["finnhub"]},
        })
        assert out == {"price_history": ["fmp"], "ratios": ["finnhub"]}


# ---------------------------------------------------------------------------
# Config-only (no ping)
# ---------------------------------------------------------------------------

class TestConfigChecks:
    def test_ok_when_key_present_and_instantiated(self, monkeypatch):
        monkeypatch.setenv("FMP_API_KEY", "abc")
        cfg = _config()
        provider_map = {"fmp": _fake_provider("fmp")}
        report = check_providers_health(cfg, provider_map, only="fmp")
        fmp = report["providers"][0]
        assert fmp["status"] == "ok"
        assert fmp["config"]["api_key_present"] is True
        assert fmp["config"]["instantiable"] is True
        assert "price_history" in fmp["config"]["tier_endpoints"]

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        cfg = _config()
        report = check_providers_health(cfg, {}, only="finnhub")
        f = report["providers"][0]
        assert f["status"] == "missing_api_key"
        assert f["config"]["api_key_present"] is False

    def test_not_instantiable(self, monkeypatch):
        # Key is present, but the provider failed to instantiate (absent from map).
        monkeypatch.setenv("FMP_API_KEY", "abc")
        cfg = _config()
        report = check_providers_health(cfg, {}, only="fmp")
        fmp = report["providers"][0]
        assert fmp["status"] == "not_instantiable"
        assert fmp["config"]["api_key_present"] is True
        assert fmp["config"]["instantiable"] is False

    def test_unused_provider(self):
        # Provider configured but not in any tier.
        cfg = _config(
            providers={
                "yfinance": ProviderConfig(name="yfinance"),
                "loner": ProviderConfig(name="loner"),
            },
            tiers={"price_history": ["yfinance"]},
        )
        provider_map = {
            "yfinance": _fake_provider("yfinance"),
            "loner": _fake_provider("loner"),
        }
        report = check_providers_health(cfg, provider_map, only="loner")
        loner = report["providers"][0]
        assert loner["status"] == "unused"
        assert loner["config"]["in_use_in_tier"] is False

    def test_yfinance_has_no_key_requirement(self):
        cfg = _config()
        report = check_providers_health(
            cfg, {"yfinance": _fake_provider("yfinance")}, only="yfinance",
        )
        y = report["providers"][0]
        assert y["status"] == "ok"
        assert y["config"]["api_key_env"] is None
        assert y["config"]["api_key_present"] is True


class TestTierIssues:
    def test_unknown_provider_in_tier(self):
        cfg = _config(
            providers={"fmp": ProviderConfig(name="fmp", api_key_env="FMP_API_KEY")},
            tiers={"price_history": ["fpm"]},  # typo
        )
        report = check_providers_health(cfg, {})
        assert {"endpoint": "price_history", "unknown_provider": "fpm"} in report["tier_issues"]

    def test_no_issues_when_all_refs_known(self):
        cfg = _config()
        report = check_providers_health(cfg, {})
        assert report["tier_issues"] == []


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------

class TestPing:
    def test_ping_ok(self, monkeypatch):
        monkeypatch.setenv("FMP_API_KEY", "abc")
        cfg = _config()
        provider_map = {"fmp": _fake_provider("fmp")}
        report = check_providers_health(cfg, provider_map, ping=True, only="fmp")
        fmp = report["providers"][0]
        assert fmp["ping"]["attempted"] is True
        assert fmp["ping"]["ok"] is True
        assert fmp["ping"]["endpoint"] == "quote"
        assert fmp["ping"]["symbol"] == "AAPL"
        assert fmp["ping"]["latency_ms"] is not None
        assert fmp["status"] == "ok"
        assert report["summary"]["pings_succeeded"] == 1
        assert report["summary"]["pings_failed"] == 0

    def test_ping_failed_downgrades_ok_status(self, monkeypatch):
        monkeypatch.setenv("FMP_API_KEY", "abc")
        cfg = _config()
        provider_map = {
            "fmp": _fake_provider(
                "fmp", quote_raises=RateLimitError(
                    provider="fmp", message="quota", retry_after_seconds=60,
                ),
            ),
        }
        report = check_providers_health(cfg, provider_map, ping=True, only="fmp")
        fmp = report["providers"][0]
        assert fmp["ping"]["ok"] is False
        assert fmp["ping"]["error"]["code"] == "PROVIDER_QUOTA_EXHAUSTED"
        assert fmp["status"] == "ping_failed"
        assert report["summary"]["ping_failed"] == 1  # status bucket
        assert report["summary"]["pings_failed"] == 1  # ping outcome counter

    def test_ping_unexpected_exception_captured(self, monkeypatch):
        monkeypatch.setenv("FMP_API_KEY", "abc")
        cfg = _config()
        provider_map = {
            "fmp": _fake_provider(
                "fmp", quote_raises=RuntimeError("boom"),
            ),
        }
        report = check_providers_health(cfg, provider_map, ping=True, only="fmp")
        fmp = report["providers"][0]
        assert fmp["ping"]["ok"] is False
        assert fmp["ping"]["error"]["code"] == "UNEXPECTED"
        assert "RuntimeError" in fmp["ping"]["error"]["message"]

    def test_ping_skipped_when_not_instantiated(self, monkeypatch):
        monkeypatch.setenv("FMP_API_KEY", "abc")
        cfg = _config()
        report = check_providers_health(cfg, {}, ping=True, only="fmp")
        fmp = report["providers"][0]
        assert fmp["ping"]["attempted"] is False
        assert fmp["status"] == "not_instantiable"

    def test_ping_off_by_default(self, monkeypatch):
        monkeypatch.setenv("FMP_API_KEY", "abc")
        cfg = _config()
        provider_map = {"fmp": _fake_provider("fmp")}
        report = check_providers_health(cfg, provider_map, only="fmp")
        fmp = report["providers"][0]
        assert fmp["ping"]["attempted"] is False
        # get_quote should not have been called
        provider_map["fmp"].get_quote.assert_not_called()


# ---------------------------------------------------------------------------
# Summary counters
# ---------------------------------------------------------------------------

class TestSummary:
    def test_summary_counts_correctly(self, monkeypatch):
        monkeypatch.setenv("FMP_API_KEY", "abc")
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        cfg = _config()
        provider_map = {
            "fmp": _fake_provider("fmp"),
            "yfinance": _fake_provider("yfinance"),
        }
        report = check_providers_health(cfg, provider_map)
        summary = report["summary"]
        assert summary["total"] == 3
        assert summary["ok"] == 2  # fmp + yfinance
        assert summary["missing_api_key"] == 1  # finnhub
        assert summary["pings_attempted"] is False

    def test_only_filter_restricts_total(self):
        cfg = _config()
        report = check_providers_health(cfg, {}, only="yfinance")
        assert report["summary"]["total"] == 1
        assert len(report["providers"]) == 1
        assert report["providers"][0]["name"] == "yfinance"
