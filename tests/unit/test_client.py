"""Unit tests for OneFinanceClient."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from onefinance.core.client import OneFinanceClient
from onefinance.core.errors import (
    AllProvidersFailedError,
    InvalidArgumentError,
    ProviderError,
)
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    DCFValuation,
    EarningsRecord,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    NewsArticle,
    OptionChain,
    PriceBar,
    Quote,
    ScreenerResult,
    SectorInfo,
)
from onefinance.providers.base import BaseProvider

NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


# -----------------------------------------------------------------------
# Test provider stubs
# -----------------------------------------------------------------------


class _FakeProvider(BaseProvider):
    """Provider that returns canned data for all endpoints."""

    name = "fake"

    def __init__(self) -> None:
        self.call_count: dict[str, int] = {}

    def _track(self, name: str) -> None:
        self.call_count[name] = self.call_count.get(name, 0) + 1

    def get_price_history(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> list[PriceBar]:
        self._track("price_history")
        return [
            PriceBar(
                symbol=symbol,
                date=date(2024, 1, 2),
                open=185.0,
                high=186.5,
                low=184.0,
                close=185.64,
                adj_close=185.64,
                volume=50_000_000,
                source=self.name,
                fetched_at=NOW,
            )
        ]

    def get_info(self, symbol: str) -> CompanyInfo:
        self._track("info")
        return CompanyInfo(
            symbol=symbol,
            name=f"{symbol} Inc.",
            source=self.name,
            fetched_at=NOW,
        )

    def get_quote(self, symbol: str) -> Quote:
        self._track("quote")
        return Quote(
            symbol=symbol,
            timestamp=NOW,
            price=185.64,
            volume=50_000_000,
            source=self.name,
            fetched_at=NOW,
        )

    def get_financials(
        self, symbol: str, statement: str = "income", period: str = "annual"
    ) -> list[IncomeStatement | BalanceSheet | CashFlow]:
        self._track("financials")
        return [
            IncomeStatement(
                symbol=symbol,
                period="FY2023",
                fiscal_date=date(2023, 9, 30),
                revenue=383_000_000_000.0,
                cost_of_revenue=214_000_000_000.0,
                gross_profit=169_000_000_000.0,
                operating_income=114_000_000_000.0,
                net_income=96_995_000_000.0,
                eps_basic=6.16,
                eps_diluted=6.13,
                currency="USD",
                source=self.name,
                fetched_at=NOW,
            )
        ]

    def get_insider_trades(self, symbol: str, since: date | None = None) -> list[InsiderTrade]:
        self._track("insider_trades")
        return [
            InsiderTrade(
                symbol=symbol,
                filing_date=date(2024, 1, 15),
                insider_name="Tim Cook",
                trade_type="sell",
                shares=50_000.0,
                source=self.name,
                fetched_at=NOW,
            )
        ]

    def get_ratios(self, symbol: str, period: str = "annual") -> list[FinancialRatios]:
        self._track("ratios")
        return [
            FinancialRatios(
                symbol=symbol,
                period="FY2023",
                fiscal_date=date(2023, 9, 30),
                source=self.name,
                fetched_at=NOW,
            )
        ]

    def get_earnings(self, symbol: str) -> list[EarningsRecord]:
        self._track("earnings")
        return [
            EarningsRecord(
                symbol=symbol,
                period="2024-Q1",
                fiscal_date=date(2024, 1, 1),
                source=self.name,
                fetched_at=NOW,
            )
        ]

    def get_dcf(self, symbol: str) -> DCFValuation:
        self._track("dcf")
        return DCFValuation(
            symbol=symbol,
            dcf=195.50,
            stock_price=185.64,
            dcf_date=date(2024, 1, 3),
            source=self.name,
            fetched_at=NOW,
        )

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        self._track("news")
        return [
            NewsArticle(
                symbol=symbol,
                title="Test headline",
                publisher="Reuters",
                link="https://reuters.com/test",
                published_at=NOW,
                source=self.name,
                fetched_at=NOW,
            )
        ]

    def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        self._track("corporate_actions")
        return [
            CorporateAction(
                symbol=symbol,
                date=date(2024, 2, 9),
                action_type="dividend",
                amount=0.24,
                source=self.name,
                fetched_at=NOW,
            )
        ]

    def get_institutional_holders(self, symbol: str) -> list[InstitutionalHolder]:
        self._track("institutional_holders")
        return [
            InstitutionalHolder(
                symbol=symbol,
                holder_name="Vanguard Group",
                shares=1_200_000_000,
                source=self.name,
                fetched_at=NOW,
            )
        ]

    def get_analyst_data(self, symbol: str) -> AnalystData:
        self._track("analyst_data")
        return AnalystData(
            symbol=symbol,
            target_mean=195.0,
            source=self.name,
            fetched_at=NOW,
        )

    def get_options_expirations(self, symbol: str) -> list[date]:
        self._track("options_expirations")
        return [date(2024, 1, 19), date(2024, 2, 16)]

    def get_option_chain(self, symbol: str, expiration: date) -> OptionChain:
        self._track("option_chain")
        return OptionChain(
            symbol=symbol,
            expiration_date=expiration,
            calls=[],
            puts=[],
            source=self.name,
            fetched_at=NOW,
        )

    def screen_stocks(self, query: str) -> list[ScreenerResult]:
        self._track("screen_stocks")
        return [
            ScreenerResult(
                symbol="AAPL",
                source=self.name,
                fetched_at=NOW,
            )
        ]

    def get_sector_overview(self, sector: str) -> SectorInfo:
        self._track("sector_overview")
        return SectorInfo(
            name=sector.title(),
            source=self.name,
            fetched_at=NOW,
        )

    def get_forward_estimates(self, symbol: str) -> list[ForwardEstimates]:
        self._track("forward_estimates")
        return [
            ForwardEstimates(
                symbol=symbol,
                period="2024-FY",
                source=self.name,
                fetched_at=NOW,
            )
        ]

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 0.0


class _FailingProvider(BaseProvider):
    """Provider that always fails."""

    name = "failing"

    def get_price_history(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> list[PriceBar]:
        raise ProviderError("NETWORK_ERROR", "network down", provider=self.name, retry_safe=True)

    def get_info(self, symbol: str) -> CompanyInfo:
        raise ProviderError("NETWORK_ERROR", "network down", provider=self.name, retry_safe=True)

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 0.0


@pytest.fixture
def fake_provider() -> _FakeProvider:
    return _FakeProvider()


@pytest.fixture
def client(fake_provider: _FakeProvider, tmp_path: Path) -> Generator[OneFinanceClient]:
    c = OneFinanceClient(
        providers=fake_provider,
        cache_dir=tmp_path / "test_cache",
        audit=False,
    )
    yield c
    c.close()


# -----------------------------------------------------------------------
# Basic fetch
# -----------------------------------------------------------------------


class TestGetPriceHistory:
    def test_returns_bars(self, client: OneFinanceClient) -> None:
        bars = client.get_price_history("AAPL", "2024-01-01", "2024-12-31")
        assert len(bars) == 1
        assert bars[0].symbol == "AAPL"
        assert bars[0].source == "fake"

    def test_date_objects_work(self, client: OneFinanceClient) -> None:
        bars = client.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31))
        assert len(bars) == 1

    def test_end_defaults_to_today(self, client: OneFinanceClient) -> None:
        bars = client.get_price_history("AAPL", "2024-01-01")
        assert len(bars) == 1

    def test_invalid_date_range(self, client: OneFinanceClient) -> None:
        with pytest.raises(InvalidArgumentError):
            client.get_price_history("AAPL", "2024-12-31", "2024-01-01")


class TestGetInfo:
    def test_returns_info(self, client: OneFinanceClient) -> None:
        info = client.get_info("AAPL")
        assert isinstance(info, CompanyInfo)
        assert info.symbol == "AAPL"
        assert info.name == "AAPL Inc."


# -----------------------------------------------------------------------
# Cache integration
# -----------------------------------------------------------------------


class TestCacheIntegration:
    def test_second_call_uses_cache(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ) -> None:
        """Second call should hit cache, not the provider."""
        client.get_info("AAPL")
        client.get_info("AAPL")
        assert fake_provider.call_count["info"] == 1  # only one provider call

    def test_no_cache_bypasses(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ) -> None:
        """no_cache=True should always hit the provider."""
        client.get_info("AAPL", no_cache=True)
        client.get_info("AAPL", no_cache=True)
        assert fake_provider.call_count["info"] == 2  # two provider calls

    def test_no_cache_does_not_write(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ) -> None:
        """no_cache=True should not write to cache either."""
        client.get_info("AAPL", no_cache=True)
        # Now fetch normally — should call provider since nothing was cached
        client.get_info("AAPL")
        assert fake_provider.call_count["info"] == 2

    def test_different_symbols_different_cache(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ) -> None:
        client.get_info("AAPL")
        client.get_info("MSFT")
        assert fake_provider.call_count["info"] == 2

    def test_cache_stats_accessible(self, client: OneFinanceClient) -> None:
        client.get_info("AAPL")
        stats = client.cache.stats()
        assert stats["entries"] >= 1

    def test_cache_invalidation(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ) -> None:
        """After invalidation, provider is called again."""
        client.get_info("AAPL")
        client.cache.invalidate_by_type("info")
        client.get_info("AAPL")
        assert fake_provider.call_count["info"] == 2


# -----------------------------------------------------------------------
# Provider selection
# -----------------------------------------------------------------------


class TestProviderSelection:
    def test_force_provider(self, tmp_path: Path) -> None:
        p1 = _FakeProvider()
        p1.name = "primary"
        p2 = _FakeProvider()
        p2.name = "secondary"
        client = OneFinanceClient(
            providers=[p1, p2],
            cache_dir=tmp_path / "cache",
            audit=False,
        )
        info = client.get_info("AAPL", provider="secondary", no_cache=True)
        assert info.source == "secondary"
        assert p2.call_count.get("info") == 1
        assert p1.call_count.get("info", 0) == 0
        client.close()

    def test_unknown_provider_raises(self, client: OneFinanceClient) -> None:
        with pytest.raises(InvalidArgumentError):
            client.get_info("AAPL", provider="nonexistent")


# -----------------------------------------------------------------------
# Provider fallback
# -----------------------------------------------------------------------


class TestProviderFallback:
    def test_falls_through_on_not_supported(self, tmp_path: Path) -> None:
        """If first provider doesn't support endpoint, try next."""

        # _MinimalProvider from test_base_provider doesn't override get_info
        class _MinimalProv(BaseProvider):
            name = "minimal"

            def is_rate_limited(self, r: Any) -> bool:
                return False

            def cooldown_for(self, r: Any) -> float:
                return 0.0

        minimal = _MinimalProv()
        fake = _FakeProvider()
        client = OneFinanceClient(
            providers=[minimal, fake],
            cache_dir=tmp_path / "cache",
            audit=False,
        )
        info = client.get_info("AAPL", no_cache=True)
        assert info.source == "fake"
        client.close()

    def test_all_providers_fail_raises(self, tmp_path: Path) -> None:
        failing = _FailingProvider()
        client = OneFinanceClient(
            providers=[failing],
            cache_dir=tmp_path / "cache",
            audit=False,
        )
        with pytest.raises(AllProvidersFailedError) as exc_info:
            client.get_info("AAPL", no_cache=True)
        assert len(exc_info.value.failures) == 1
        client.close()


# -----------------------------------------------------------------------
# Per-call TTL override
# -----------------------------------------------------------------------


class TestTTLOverride:
    def test_custom_ttl(self, client: OneFinanceClient, fake_provider: _FakeProvider) -> None:
        """Custom TTL should be passed through to cache."""
        # Just verify it doesn't error — TTL correctness is tested in cache tests
        client.get_info("AAPL", ttl=60)
        assert fake_provider.call_count["info"] == 1


# -----------------------------------------------------------------------
# Context manager
# -----------------------------------------------------------------------


class TestContextManager:
    def test_with_statement(self, tmp_path: Path) -> None:
        with OneFinanceClient(
            providers=_FakeProvider(),
            cache_dir=tmp_path / "cache",
            audit=False,
        ) as client:
            info = client.get_info("AAPL")
            assert info.symbol == "AAPL"


# -----------------------------------------------------------------------
# Audit properties
# -----------------------------------------------------------------------


class TestAuditAccess:
    def test_audit_log_property(self, client: OneFinanceClient) -> None:
        from onefinance.audit.log import AuditLog

        assert isinstance(client.audit_log, AuditLog)

    def test_audit_stats(self, client: OneFinanceClient) -> None:
        from onefinance.audit.models import AuditStats

        stats = client.audit_stats()
        assert isinstance(stats, AuditStats)

    def test_providers_property(self, client: OneFinanceClient) -> None:
        from onefinance.core.router import ProviderRouter

        assert isinstance(client.providers, ProviderRouter)


# -----------------------------------------------------------------------
# All remaining endpoint methods
# -----------------------------------------------------------------------


class TestGetQuote:
    def test_returns_quote(self, client: OneFinanceClient) -> None:
        q = client.get_quote("AAPL", no_cache=True)
        assert isinstance(q, Quote)
        assert q.symbol == "AAPL"
        assert q.price == 185.64

    def test_cached_second_call(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ) -> None:
        client.get_quote("AAPL")
        client.get_quote("AAPL")
        assert fake_provider.call_count["quote"] == 1


class TestGetFinancials:
    def test_returns_financials(self, client: OneFinanceClient) -> None:
        results = client.get_financials("AAPL", no_cache=True)
        assert len(results) == 1
        assert isinstance(results[0], IncomeStatement)


class TestGetInsiderTrades:
    def test_returns_trades(self, client: OneFinanceClient) -> None:
        results = client.get_insider_trades("AAPL", no_cache=True)
        assert len(results) == 1
        assert results[0].insider_name == "Tim Cook"

    def test_with_since_date(self, client: OneFinanceClient) -> None:
        results = client.get_insider_trades("AAPL", since="2024-01-01", no_cache=True)
        assert len(results) == 1

    def test_with_since_date_object(self, client: OneFinanceClient) -> None:
        results = client.get_insider_trades("AAPL", since=date(2024, 1, 1), no_cache=True)
        assert len(results) == 1


class TestGetRatios:
    def test_returns_ratios(self, client: OneFinanceClient) -> None:
        results = client.get_ratios("AAPL", no_cache=True)
        assert len(results) == 1
        assert isinstance(results[0], FinancialRatios)

    def test_fresh_flag(self, client: OneFinanceClient) -> None:
        results = client.get_ratios("AAPL", fresh=True, no_cache=True)
        assert len(results) == 1


class TestGetEarnings:
    def test_returns_earnings(self, client: OneFinanceClient) -> None:
        results = client.get_earnings("AAPL", no_cache=True)
        assert len(results) == 1
        assert isinstance(results[0], EarningsRecord)

    def test_fresh_flag(self, client: OneFinanceClient) -> None:
        results = client.get_earnings("AAPL", fresh=True, no_cache=True)
        assert len(results) == 1


class TestGetDcf:
    def test_returns_dcf(self, client: OneFinanceClient) -> None:
        result = client.get_dcf("AAPL", no_cache=True)
        assert isinstance(result, DCFValuation)
        assert result.symbol == "AAPL"
        assert result.dcf == 195.50


class TestGetNews:
    def test_returns_news(self, client: OneFinanceClient) -> None:
        articles = client.get_news("AAPL", no_cache=True)
        assert len(articles) == 1
        assert isinstance(articles[0], NewsArticle)


class TestGetCorporateActions:
    def test_returns_actions(self, client: OneFinanceClient) -> None:
        actions = client.get_corporate_actions("AAPL", no_cache=True)
        assert len(actions) == 1
        assert isinstance(actions[0], CorporateAction)


class TestGetInstitutionalHolders:
    def test_returns_holders(self, client: OneFinanceClient) -> None:
        holders = client.get_institutional_holders("AAPL", no_cache=True)
        assert len(holders) == 1
        assert isinstance(holders[0], InstitutionalHolder)


class TestGetAnalystData:
    def test_returns_analyst_data(self, client: OneFinanceClient) -> None:
        data = client.get_analyst_data("AAPL", no_cache=True)
        assert isinstance(data, AnalystData)
        assert data.symbol == "AAPL"


class TestGetOptionsExpirations:
    def test_returns_dates(self, client: OneFinanceClient) -> None:
        dates = client.get_options_expirations("AAPL", no_cache=True)
        assert len(dates) == 2
        assert all(isinstance(d, date) for d in dates)


class TestGetOptionChain:
    def test_returns_chain(self, client: OneFinanceClient) -> None:
        chain = client.get_option_chain("AAPL", date(2024, 1, 19), no_cache=True)
        assert isinstance(chain, OptionChain)
        assert chain.symbol == "AAPL"


class TestScreenStocks:
    def test_returns_results(self, client: OneFinanceClient) -> None:
        results = client.screen_stocks("sector=Technology", no_cache=True)
        assert len(results) == 1
        assert isinstance(results[0], ScreenerResult)


class TestGetSectorOverview:
    def test_returns_sector_info(self, client: OneFinanceClient) -> None:
        info = client.get_sector_overview("technology", no_cache=True)
        assert isinstance(info, SectorInfo)
        assert info.name == "Technology"


class TestGetForwardEstimates:
    def test_returns_estimates(self, client: OneFinanceClient) -> None:
        results = client.get_forward_estimates("AAPL", no_cache=True)
        assert len(results) == 1
        assert isinstance(results[0], ForwardEstimates)


# -----------------------------------------------------------------------
# check_providers
# -----------------------------------------------------------------------


class TestCheckProviders:
    def test_returns_report(self, client: OneFinanceClient) -> None:
        report = client.check_providers()
        assert isinstance(report, dict)
        assert "providers" in report
