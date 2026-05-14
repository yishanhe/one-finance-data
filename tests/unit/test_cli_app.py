"""CLI integration tests using Typer CliRunner."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
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

NOW = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone.utc)


def _make_bars(n: int = 2) -> list[PriceBar]:
    return [
        PriceBar(
            symbol="AAPL",
            date=date(2024, 1, i + 1),
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
    def test_returns_json_envelope(self):
        bars = _make_bars(2)
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_price_history.return_value = bars
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["price", "AAPL", "--start", "2024-01-01", "--end", "2024-01-03"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["command"] == "price"
        assert len(data["data"]) == 2
        assert data["data"][0]["symbol"] == "AAPL"

    def test_range_flag(self):
        bars = _make_bars(1)
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_price_history.return_value = bars
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["price", "AAPL", "--range", "1m"])
        assert result.exit_code == 0

    def test_invalid_range_exits_1(self):
        result = runner.invoke(app, ["price", "AAPL", "--range", "invalid"])
        assert result.exit_code == 1

    def test_config_error_exits_4(self):
        with patch("onefinance.cli.app._make_client",
                   side_effect=ConfigError("Missing key")):
            result = runner.invoke(app, ["price", "AAPL", "--range", "1m"])
        assert result.exit_code == 4
        data = json.loads(result.output)
        assert data["status"] == "error"

    def test_rate_limit_exits_2(self):
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_price_history.side_effect = RateLimitError(
                provider="fmp", message="Quota hit", retry_after_seconds=3600
            )
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["price", "AAPL", "--range", "1m"])
        assert result.exit_code == 2

    def test_dry_run_returns_plan(self):
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
        symbol="AAPL", period="2023-FY", fiscal_date=date(2023, 9, 30),
        revenue=383_285_000_000, cost_of_revenue=214_137_000_000,
        gross_profit=169_148_000_000, operating_income=114_301_000_000,
        net_income=96_995_000_000, eps_basic=6.16, eps_diluted=6.13,
        currency="USD", source="fmp", fetched_at=NOW,
    )


def _make_info() -> CompanyInfo:
    return CompanyInfo(
        symbol="AAPL", name="Apple Inc", exchange="NASDAQ",
        sector="Technology", industry="Consumer Electronics",
        country="US", market_cap=2_900_000_000_000, description=None,
        website="https://www.apple.com", employees=164_000,
        currency="USD", source="fmp", fetched_at=NOW,
    )


def _make_trade() -> InsiderTrade:
    return InsiderTrade(
        symbol="AAPL", filing_date=date(2024, 1, 15), trade_date=date(2024, 1, 12),
        insider_name="Tim Cook", insider_title="CEO", trade_type="sell",
        shares=50_000, price_per_share=185.64, total_value=9_282_000,
        shares_owned_after=3_000_000, source="fmp", fetched_at=NOW,
    )


def _make_ratios() -> FinancialRatios:
    return FinancialRatios(
        symbol="AAPL", period="2023-FY", fiscal_date=date(2023, 9, 30),
        pe_ratio=29.5, pb_ratio=47.3, ps_ratio=7.8, debt_to_equity=1.79,
        current_ratio=0.99, return_on_equity=1.56, return_on_assets=0.28,
        gross_margin=0.443, operating_margin=0.298, net_margin=0.253,
        dividend_yield=0.0051, source="fmp", fetched_at=NOW,
    )


def _make_earnings() -> EarningsRecord:
    return EarningsRecord(
        symbol="AAPL", period="2024-Q2", fiscal_date=date(2024, 3, 31),
        eps_actual=2.18, eps_estimate=2.10, eps_surprise=0.08,
        revenue_actual=None, revenue_estimate=None, source="fmp", fetched_at=NOW,
    )


# -----------------------------------------------------------------------
# quote
# -----------------------------------------------------------------------

class TestQuoteCommand:
    def test_returns_json_envelope(self):
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

    def test_provider_error_exits_2(self):
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
# financials
# -----------------------------------------------------------------------

class TestFinancialsCommand:
    def test_returns_list(self):
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_financials.return_value = [_make_income()]
            mock_client_fn.return_value = client
            result = runner.invoke(app, [
                "financials", "AAPL", "--statement", "income", "--period", "annual"
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert len(data["data"]) == 1

    def test_invalid_statement_exits_1(self):
        result = runner.invoke(app, [
            "financials", "AAPL", "--statement", "invalid", "--period", "annual"
        ])
        assert result.exit_code == 1


# -----------------------------------------------------------------------
# info
# -----------------------------------------------------------------------

class TestInfoCommand:
    def test_returns_company_info(self):
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
    def test_returns_trades(self):
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
    def test_returns_ratios(self):
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_ratios.return_value = [_make_ratios()]
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["ratios", "AAPL", "--period", "annual"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"][0]["pe_ratio"] == 29.5

    def test_fresh_flag_passed_to_client(self):
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_ratios.return_value = [_make_ratios()]
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["ratios", "AAPL", "--period", "annual", "--fresh"])
        assert result.exit_code == 0
        client.get_ratios.assert_called_once_with(
            "AAPL", period="annual", fresh=True,
            no_cache=False, provider=None,
        )


# -----------------------------------------------------------------------
# earnings
# -----------------------------------------------------------------------

class TestEarningsCommand:
    def test_returns_earnings(self):
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.get_earnings.return_value = [_make_earnings()]
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["earnings", "AAPL"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"][0]["eps_actual"] == 2.18


# -----------------------------------------------------------------------
# capabilities and version (M10)
# -----------------------------------------------------------------------

class TestCapabilitiesCommand:
    def test_returns_manifest(self):
        result = runner.invoke(app, ["capabilities"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == "1.0"
        assert "commands" in data
        commands = {c["name"] for c in data["commands"]}
        assert "price" in commands
        assert "quote" in commands
        assert "ratios" in commands

    def test_each_command_has_required_fields(self):
        result = runner.invoke(app, ["capabilities"])
        data = json.loads(result.output)
        for cmd in data["commands"]:
            assert "name" in cmd
            assert "description" in cmd
            assert "freshness_type" in cmd
            assert "arguments" in cmd
            assert "examples" in cmd


class TestVersionCommand:
    def test_returns_version(self):
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
    def test_returns_stats(self):
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.cache.stats.return_value = {"entries": 42, "size_mb": 1.5, "hits": 10, "misses": 5}
            mock_client_fn.return_value = client
            result = runner.invoke(app, ["cache", "stats"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["entries"] == 42


class TestProvidersStatusCommand:
    def test_returns_status(self):
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


class TestConfigShowCommand:
    def test_returns_config(self):
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

    def _dry_run(self, *args):
        with patch("onefinance.cli.app._make_client") as mock_client_fn:
            client = MagicMock()
            client.cache.get.return_value = None
            mock_client_fn.return_value = client
            return runner.invoke(app, list(args) + ["--dry-run"])

    def test_quote_dry_run(self):
        result = self._dry_run("quote", "AAPL")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "dry_run"

    def test_financials_dry_run(self):
        result = self._dry_run("financials", "AAPL", "--statement", "income", "--period", "annual")
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "dry_run"

    def test_info_dry_run(self):
        result = self._dry_run("info", "AAPL")
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "dry_run"

    def test_insiders_dry_run(self):
        result = self._dry_run("insiders", "AAPL")
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "dry_run"

    def test_ratios_dry_run(self):
        result = self._dry_run("ratios", "AAPL", "--period", "annual")
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "dry_run"

    def test_earnings_dry_run(self):
        result = self._dry_run("earnings", "AAPL")
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "dry_run"
