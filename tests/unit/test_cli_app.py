"""CLI integration tests using Typer CliRunner."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

from click.testing import Result
from typer.testing import CliRunner

from onefinance.cli.app import app
from onefinance.core.errors import ConfigError, ProviderError, RateLimitError
from onefinance.core.models import (
    CompanyInfo,
    EarningsRecord,
    FinancialRatios,
    IncomeStatement,
    InsiderTrade,
    PriceBar,
    Quote,
)

runner = CliRunner()

NOW = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)


def _make_bars(n: int = 2) -> list[PriceBar]:
    from datetime import timedelta as _td

    base = date(2024, 1, 1)
    return [
        PriceBar(
            symbol="AAPL",
            date=base + _td(days=i),
            open=184.0 + i,
            high=186.0 + i,
            low=183.0 + i,
            close=185.0 + i,
            adj_close=185.0 + i,
            volume=50_000_000,
            source="fmp",
            fetched_at=NOW,
        )
        for i in range(n)
    ]


class TestPriceCommand:
    def test_returns_json_envelope(self) -> None:
        bars = _make_bars(2)
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_price_history.return_value = bars
            mock_client_fn.return_value = client
            result = runner.invoke(
                app, ["price", "AAPL", "--start", "2024-01-01", "--end", "2024-01-03"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["command"] == "price"
        assert len(data["data"]) == 2
        assert data["data"][0]["symbol"] == "AAPL"

    def test_range_flag(self) -> None:
        bars = _make_bars(1)
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_price_history.return_value = bars
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["price", "AAPL", "--range", "1m"])
        assert result.exit_code == 0

    def test_invalid_range_exits_1(self) -> None:
        result = runner.invoke(app, ["price", "AAPL", "--range", "invalid"])
        assert result.exit_code == 1

    def test_config_error_exits_4(self) -> None:
        with patch("onefinance.cli.app._make_client", side_effect=ConfigError("Missing key")):
            result = runner.invoke(app, ["price", "AAPL", "--range", "1m"])
        assert result.exit_code == 4
        data = json.loads(result.output)
        assert data["status"] == "error"

    def test_rate_limit_exits_2(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_price_history.side_effect = RateLimitError(
                provider="fmp", message="Quota hit", retry_after_seconds=3600
            )
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["price", "AAPL", "--range", "1m"])
        assert result.exit_code == 2

    def test_dry_run_returns_plan(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.cache.get.return_value = None
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["price", "AAPL", "--range", "1m", "--dry-run"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "dry_run"
        assert "plan" in data


# -----------------------------------------------------------------------
# Helper factories
# -----------------------------------------------------------------------


def _make_quote() -> Quote:
    return Quote(
        symbol="AAPL",
        timestamp=NOW,
        price=185.64,
        bid=None,
        ask=None,
        volume=52_000_000,
        source="fmp",
        fetched_at=NOW,
    )


def _make_income() -> IncomeStatement:
    return IncomeStatement(
        symbol="AAPL",
        period="2023-FY",
        fiscal_date=date(2023, 9, 30),
        revenue=383_285_000_000,
        cost_of_revenue=214_137_000_000,
        gross_profit=169_148_000_000,
        operating_income=114_301_000_000,
        net_income=96_995_000_000,
        eps_basic=6.16,
        eps_diluted=6.13,
        currency="USD",
        source="fmp",
        fetched_at=NOW,
    )


def _make_info() -> CompanyInfo:
    return CompanyInfo(
        symbol="AAPL",
        name="Apple Inc",
        exchange="NASDAQ",
        sector="Technology",
        industry="Consumer Electronics",
        country="US",
        market_cap=2_900_000_000_000,
        description=None,
        website="https://www.apple.com",
        employees=164_000,
        currency="USD",
        source="fmp",
        fetched_at=NOW,
    )


def _make_trade() -> InsiderTrade:
    return InsiderTrade(
        symbol="AAPL",
        filing_date=date(2024, 1, 15),
        trade_date=date(2024, 1, 12),
        insider_name="Tim Cook",
        insider_title="CEO",
        trade_type="sell",
        shares=50_000,
        price_per_share=185.64,
        total_value=9_282_000,
        shares_owned_after=3_000_000,
        source="fmp",
        fetched_at=NOW,
    )


def _make_ratios() -> FinancialRatios:
    return FinancialRatios(
        symbol="AAPL",
        period="2023-FY",
        fiscal_date=date(2023, 9, 30),
        pe_ratio=29.5,
        pb_ratio=47.3,
        ps_ratio=7.8,
        debt_to_equity=1.79,
        current_ratio=0.99,
        return_on_equity=1.56,
        return_on_assets=0.28,
        gross_margin=0.443,
        operating_margin=0.298,
        net_margin=0.253,
        dividend_yield=0.0051,
        source="fmp",
        fetched_at=NOW,
    )


def _make_earnings() -> EarningsRecord:
    return EarningsRecord(
        symbol="AAPL",
        period="2024-Q2",
        fiscal_date=date(2024, 3, 31),
        eps_actual=2.18,
        eps_estimate=2.10,
        eps_surprise=0.08,
        revenue_actual=None,
        revenue_estimate=None,
        source="fmp",
        fetched_at=NOW,
    )


# -----------------------------------------------------------------------
# quote
# -----------------------------------------------------------------------


class TestQuoteCommand:
    def test_returns_json_envelope(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_quote.return_value = _make_quote()
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["quote", "AAPL"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["command"] == "quote"
        assert data["data"]["price"] == 185.64

    def test_provider_error_exits_2(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_quote.side_effect = ProviderError(
                code="SYMBOL_NOT_FOUND",
                message="Not found",
                provider="fmp",
                retry_safe=False,
            )
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["quote", "FAKE"])
        assert result.exit_code == 2


# -----------------------------------------------------------------------
# quotes (batch)
# -----------------------------------------------------------------------


class TestQuotesCommand:
    def test_returns_json_envelope(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            q1 = _make_quote()
            q2 = q1.model_copy(update={"symbol": "MSFT"})
            client.get_quotes.return_value = [q1, q2]
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["quotes", "AAPL", "MSFT"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["command"] == "quotes"
        assert len(data["data"]) == 2
        assert data["data"][0]["symbol"] == "AAPL"
        assert data["data"][1]["symbol"] == "MSFT"

    def test_partial_errors(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            q1 = _make_quote()
            err = ProviderError("NETWORK_ERROR", "Down", provider="fmp")
            client.get_quotes.return_value = [q1, err]
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["quotes", "AAPL", "FAKE"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]) == 1
        assert data["data"][0]["symbol"] == "AAPL"

        assert "errors" in data["metadata"]
        assert len(data["metadata"]["errors"]) == 1
        assert "FAKE" in data["metadata"]["errors"]
        assert data["metadata"]["errors"]["FAKE"] == "Down"


# -----------------------------------------------------------------------
# financials
# -----------------------------------------------------------------------


class TestFinancialsCommand:
    def test_returns_list(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_financials.return_value = [_make_income()]
            mock_client_fn.return_value = client
            result = runner.invoke(
                app, ["financials", "AAPL", "--statement", "income", "--period", "annual"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert len(data["data"]) == 1

    def test_invalid_statement_exits_1(self) -> None:
        result = runner.invoke(
            app, ["financials", "AAPL", "--statement", "invalid", "--period", "annual"]
        )
        assert result.exit_code == 1


# -----------------------------------------------------------------------
# info
# -----------------------------------------------------------------------


class TestInfoCommand:
    def test_returns_company_info(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_info.return_value = _make_info()
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["info", "AAPL"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["name"] == "Apple Inc"


# -----------------------------------------------------------------------
# insiders
# -----------------------------------------------------------------------


class TestInsidersCommand:
    def test_returns_trades(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_insider_trades.return_value = [_make_trade()]
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["insiders", "AAPL"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"][0]["insider_name"] == "Tim Cook"


# -----------------------------------------------------------------------
# ratios
# -----------------------------------------------------------------------


class TestRatiosCommand:
    def test_returns_ratios(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_ratios.return_value = [_make_ratios()]
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["ratios", "AAPL", "--period", "annual"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"][0]["pe_ratio"] == 29.5

    def test_fresh_flag_passed_to_client(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_ratios.return_value = [_make_ratios()]
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["ratios", "AAPL", "--period", "annual", "--fresh"])
        assert result.exit_code == 0
        client.get_ratios.assert_called_once_with(
            "AAPL",
            period="annual",
            fresh=True,
            no_cache=False,
            provider=None,
        )


# -----------------------------------------------------------------------
# earnings
# -----------------------------------------------------------------------


class TestEarningsCommand:
    def test_returns_earnings(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_earnings.return_value = [_make_earnings()]
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["earnings", "AAPL"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"][0]["eps_actual"] == 2.18


# -----------------------------------------------------------------------
# indicators
# -----------------------------------------------------------------------


class TestIndicatorsCommand:
    def test_returns_json_envelope(self) -> None:
        from onefinance.indicators.core import TechnicalIndicators

        bars = _make_bars(70)
        ind = TechnicalIndicators(
            ma5=185.3,
            ma10=184.7,
            ma20=182.1,
            ma60=178.4,
            bias_ma5=1.2,
            bias_status="safe",
            ma_alignment="bullish",
            trend_status="BULL",
            rsi14=58.4,
            macd_dif=0.82,
            macd_dea=0.51,
            macd_bar=0.62,
            atr14=3.21,
            atr_pct=1.73,
            volume_ratio=1.05,
        )
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_indicators.return_value = ind
            client.get_price_history.return_value = bars
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["indicators", "AAPL", "--range", "6m"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["command"] == "indicators"
        assert data["data"]["symbol"] == "AAPL"
        assert data["data"]["ma5"] == 185.3
        assert data["data"]["trend_status"] == "BULL"
        assert data["data"]["rsi14"] == 58.4
        assert "as_of" in data["data"]
        assert data["metadata"]["bars"] == len(bars)
        assert data["metadata"]["source"] == "fmp"

    def test_default_uses_6m_range(self) -> None:
        from datetime import timedelta as _td

        from onefinance.indicators.core import TechnicalIndicators

        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_indicators.return_value = TechnicalIndicators()
            client.get_price_history.return_value = _make_bars(70)
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["indicators", "AAPL"])
        assert result.exit_code == 0
        call_kwargs = client.get_indicators.call_args.kwargs
        delta = call_kwargs["end"] - call_kwargs["start"]
        # The CLI defaults to --range 6m, which maps to 180 days in _range_map.
        assert delta == _td(days=180)

    def test_dry_run_returns_plan(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.cache.get.return_value = None
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["indicators", "AAPL", "--dry-run"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "dry_run"

    def test_too_few_bars_exits_1(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_indicators.side_effect = ValueError("Need at least 5 bars, got 2")
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["indicators", "AAPL", "--range", "1m"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "error"


# -----------------------------------------------------------------------
# capabilities and version (M10)
# -----------------------------------------------------------------------


class TestCapabilitiesCommand:
    def test_returns_manifest(self) -> None:
        result = runner.invoke(app, ["capabilities"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == "1.0"
        assert "commands" in data
        commands = {c["name"] for c in data["commands"]}
        assert "price" in commands
        assert "quote" in commands
        assert "ratios" in commands
        assert "indicators" in commands
        assert "providers check" in commands

    def test_indicators_manifest_documents_fields(self) -> None:
        """The indicators command manifest must enumerate every returned field."""
        result = runner.invoke(app, ["capabilities"])
        data = json.loads(result.output)
        ind_cmd = next(c for c in data["commands"] if c["name"] == "indicators")
        assert "indicators" in ind_cmd, "manifest must list returned indicator fields"
        field_names = {f["name"] for f in ind_cmd["indicators"]}
        # The core schema agents rely on:
        for required in (
            "ma5",
            "ma10",
            "ma20",
            "ma60",
            "bias_ma5",
            "bias_status",
            "ma_alignment",
            "trend_status",
            "macd_dif",
            "macd_dea",
            "macd_bar",
            "rsi14",
            "atr14",
            "atr_pct",
            "volume_ratio",
            "support_levels",
            "resistance_levels",
        ):
            assert required in field_names, f"missing {required} in capabilities"

    def test_each_command_has_required_fields(self) -> None:
        result = runner.invoke(app, ["capabilities"])
        data = json.loads(result.output)
        for cmd in data["commands"]:
            assert "name" in cmd
            assert "description" in cmd
            assert "freshness_type" in cmd
            assert "arguments" in cmd
            assert "examples" in cmd


class TestVersionCommand:
    def test_returns_version(self) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "version" in data
        assert "schema_version" in data
        assert data["schema_version"] == "1.0"


# -----------------------------------------------------------------------
# cache stats, providers status, config show (M10)
# -----------------------------------------------------------------------


class TestCacheStatsCommand:
    def test_returns_stats(self) -> None:
        from onefinance.audit.models import AuditStats

        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.cache.stats.return_value = {
                "entries": 42,
                "size_mb": 1.5,
                "size_limit_bytes": 2 * 1024**3,
                "hits": 10,
                "misses": 5,
                "hit_rate": 0.667,
            }
            client.audit_stats.return_value = AuditStats(
                total_calls=30,
                cache_hits=20,
                cache_hit_rate=0.4,
                calls_by_provider={"fmp": 20, "yfinance": 10},
                errors_by_provider={"fmp": 1},
                avg_latency_ms_by_provider={"fmp": 145.2, "yfinance": 280.0},
            )
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["cache", "stats"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["entries"] == 42
        assert "hit_rate" in data
        assert "provider_usage" in data
        assert data["provider_usage"]["calls_by_provider"]["fmp"] == 20


class TestProvidersStatusCommand:
    def test_returns_status(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.providers.state.return_value = {
                "fmp": {"cooldown_until": 0, "consecutive_failures": 0}
            }
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["providers", "status"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "fmp" in data


class TestProvidersCheckCommand:
    def _report(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "providers": [
                {
                    "name": "fmp",
                    "config": {
                        "api_key_env": "FMP_API_KEY",
                        "api_key_present": True,
                        "instantiable": True,
                        "in_use_in_tier": True,
                        "tier_endpoints": ["price_history", "quote"],
                    },
                    "ping": {
                        "attempted": False,
                        "ok": None,
                        "latency_ms": None,
                        "endpoint": None,
                        "symbol": None,
                        "error": None,
                    },
                    "status": "ok",
                },
            ],
            "tier_issues": [],
            "summary": {
                "total": 1,
                "ok": 1,
                "missing_api_key": 0,
                "not_instantiable": 0,
                "unused": 0,
                "ping_failed": 0,
                "pings_succeeded": 0,
                "pings_failed": 0,
                "pings_attempted": False,
                "ping_timeout_s": None,
            },
        }
        base.update(overrides)
        return base

    def test_returns_envelope(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.check_providers.return_value = self._report()
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["providers", "check"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["command"] == "providers check"
        assert data["data"]["summary"]["total"] == 1
        assert data["metadata"]["pings_attempted"] is False

    def test_ping_flag_passed_to_client(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.check_providers.return_value = self._report()
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["providers", "check", "--ping"])
        assert result.exit_code == 0
        call_kwargs = client.check_providers.call_args.kwargs
        assert call_kwargs["ping"] is True

    def test_provider_filter_passed_as_only(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.check_providers.return_value = self._report()
            mock_client_fn.return_value = client
            result = runner.invoke(
                app,
                ["providers", "check", "--provider", "fmp"],
            )
        assert result.exit_code == 0
        assert client.check_providers.call_args.kwargs["only"] == "fmp"

    def test_exit_zero_even_when_providers_unhealthy(self) -> None:
        unhealthy = self._report(
            providers=[
                {
                    "name": "finnhub",
                    "config": {
                        "api_key_env": "FINNHUB_API_KEY",
                        "api_key_present": False,
                        "instantiable": False,
                        "in_use_in_tier": True,
                        "tier_endpoints": ["quote"],
                    },
                    "ping": {
                        "attempted": False,
                        "ok": None,
                        "latency_ms": None,
                        "endpoint": None,
                        "symbol": None,
                        "error": None,
                    },
                    "status": "missing_api_key",
                },
            ],
            summary={
                "total": 1,
                "ok": 0,
                "missing_api_key": 1,
                "not_instantiable": 0,
                "unused": 0,
                "ping_failed": 0,
                "pings_succeeded": 0,
                "pings_failed": 0,
                "pings_attempted": False,
                "ping_timeout_s": None,
            },
        )
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.check_providers.return_value = unhealthy
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["providers", "check"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["providers"][0]["status"] == "missing_api_key"


class TestConfigShowCommand:
    def test_returns_config(self) -> None:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client._config.tiers = {"price_history": ["fmp", "yfinance"]}
            client._config.cache.dir = "~/.finance_cache"
            client._config.cache.size_limit_gb = 2.0
            client._config.cooldown.default_initial_s = 60
            client._config.cooldown.max_backoff_s = 3600
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "tiers" in data or "cache" in data


# -----------------------------------------------------------------------
# --dry-run on all data commands (M10)
# -----------------------------------------------------------------------


class TestDryRunOnAllCommands:
    """--dry-run must work on every data-fetching command."""

    def _dry_run(self, *args: str) -> Result:
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.cache.get.return_value = None
            mock_client_fn.return_value = client
            return runner.invoke(app, list(args) + ["--dry-run"])

    def test_quote_dry_run(self) -> None:
        result = self._dry_run("quote", "AAPL")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "dry_run"

    def test_financials_dry_run(self) -> None:
        result = self._dry_run("financials", "AAPL", "--statement", "income", "--period", "annual")
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "dry_run"

    def test_info_dry_run(self) -> None:
        result = self._dry_run("info", "AAPL")
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "dry_run"

    def test_insiders_dry_run(self) -> None:
        result = self._dry_run("insiders", "AAPL")
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "dry_run"

    def test_ratios_dry_run(self) -> None:
        result = self._dry_run("ratios", "AAPL", "--period", "annual")
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "dry_run"

    def test_earnings_dry_run(self) -> None:
        result = self._dry_run("earnings", "AAPL")
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "dry_run"

    def test_indicators_dry_run(self) -> None:
        result = self._dry_run("indicators", "AAPL", "--range", "6m")
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "dry_run"
