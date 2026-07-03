"""Unit tests for OneFinanceClient."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from onefinance.core.client import OneFinanceClient
from onefinance.core.config import AugmentConfig, OneFinanceConfig, StaleConfig
from onefinance.core.errors import (
    AllProvidersFailedError,
    FinanceError,
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
    EarningsCalendarEntry,
    EarningsRecord,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    MarketSentiment,
    NewsArticle,
    OptionChain,
    PriceBar,
    Quote,
    ScreenerResult,
    SectorInfo,
    ShortInterest,
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

    def get_earnings_calendar(
        self, start: date | None = None, end: date | None = None
    ) -> list[EarningsCalendarEntry]:
        self._track("earnings_calendar")
        return [
            EarningsCalendarEntry(
                symbol="AAPL",
                report_date=date(2026, 7, 24),
                source=self.name,
                fetched_at=NOW,
            ),
            EarningsCalendarEntry(
                symbol="MSFT",
                report_date=date(2026, 7, 23),
                source=self.name,
                fetched_at=NOW,
            ),
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


class _ZeroPriceQuoteProvider(BaseProvider):
    """Provider that returns HTTP-200-but-garbage price=0 quotes (like A1)."""

    name = "zero_price"

    def get_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol,
            timestamp=NOW,
            price=0.0,
            volume=0,
            source=self.name,
            fetched_at=NOW,
        )

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.get_quote(s) for s in symbols]

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 0.0


class _FlakyProvider(BaseProvider):
    """Provider whose calls succeed until ``fail`` is flipped to True."""

    name = "flaky"

    def __init__(self) -> None:
        self.fail = False

    def get_info(self, symbol: str) -> CompanyInfo:
        if self.fail:
            raise ProviderError(
                "NETWORK_ERROR", "network down", provider=self.name, retry_safe=True
            )
        return CompanyInfo(symbol=symbol, name=f"{symbol} Inc.", source=self.name, fetched_at=NOW)

    def get_price_history(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> list[PriceBar]:
        if self.fail:
            raise ProviderError(
                "NETWORK_ERROR", "network down", provider=self.name, retry_safe=True
            )
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

    def get_forward_estimates(self, symbol: str) -> list[ForwardEstimates]:
        if self.fail:
            raise ProviderError(
                "NETWORK_ERROR", "network down", provider=self.name, retry_safe=True
            )
        return [
            ForwardEstimates(
                symbol=symbol,
                period="+1y",
                eps_estimate=7.0,
                source=self.name,
                fetched_at=NOW,
            )
        ]

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

    def test_no_cache_still_writes(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ) -> None:
        """no_cache=True skips read but still writes; subsequent normal fetch is a cache hit."""
        client.get_info("AAPL", no_cache=True)
        # Second call (no no_cache) should hit the cache written by the first call
        client.get_info("AAPL")
        assert fake_provider.call_count["info"] == 1

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
# Stale-on-error (last-known-good) fallback
# -----------------------------------------------------------------------


class TestStaleOnError:
    def test_serves_last_known_good_when_all_fail(self, tmp_path: Path) -> None:
        """An eligible endpoint serves its LKG copy once every provider fails."""
        flaky = _FlakyProvider()
        client = OneFinanceClient(
            providers=[flaky],
            cache_dir=tmp_path / "cache",
            audit=False,
        )
        # Prime the last-known-good copy with a successful fetch.
        first = client.get_info("AAPL")
        assert first.source == "flaky"

        # Now every provider fails; no_cache forces past the primary cache read,
        # so the only thing that can answer is the stale LKG copy.
        flaky.fail = True
        stale = client.get_info("AAPL", no_cache=True)
        assert stale.symbol == "AAPL"
        assert stale.fetched_at == NOW  # original fetch time preserved
        client.close()

    def test_no_lkg_still_raises(self, tmp_path: Path) -> None:
        """With no prior success there is no LKG copy, so the error propagates."""
        failing = _FailingProvider()
        client = OneFinanceClient(
            providers=[failing],
            cache_dir=tmp_path / "cache",
            audit=False,
        )
        with pytest.raises(AllProvidersFailedError):
            client.get_info("AAPL", no_cache=True)
        client.close()

    def test_disabled_config_raises(self, tmp_path: Path) -> None:
        """With stale fallback disabled, a primed endpoint still raises on failure."""
        flaky = _FlakyProvider()
        client = OneFinanceClient(
            providers=[flaky],
            config=OneFinanceConfig(stale=StaleConfig(enabled=False)),
            cache_dir=tmp_path / "cache",
            audit=False,
        )
        client.get_info("AAPL")
        flaky.fail = True
        with pytest.raises(AllProvidersFailedError):
            client.get_info("AAPL", no_cache=True)
        client.close()

    def test_date_keyed_endpoint_serves_across_days(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A date-keyed endpoint (estimates) must serve its LKG copy on a *later* day.

        The primary ``cache_key`` embeds ``date.today()`` and rolls over daily,
        so the LKG copy is keyed off a date-free key. Without that, the stale
        fallback would only ever hit within the same day it was written —
        useless for "providers down today, give me yesterday's data".
        """
        flaky = _FlakyProvider()
        client = OneFinanceClient(
            providers=[flaky],
            cache_dir=tmp_path / "cache",
            audit=False,
        )
        # Day 1: prime the LKG copy.
        client.get_forward_estimates("AAPL")

        # Advance the clock to a later day so the primary cache_key changes.
        class _LaterDate(date):
            @classmethod
            def today(cls) -> _LaterDate:
                return cls(2099, 1, 1)

        monkeypatch.setattr("onefinance.core.client.date", _LaterDate)

        flaky.fail = True
        stale = client.get_forward_estimates("AAPL", no_cache=True)
        assert len(stale) == 1
        assert stale[0].symbol == "AAPL"
        assert stale[0].eps_estimate == 7.0
        client.close()

    def test_ineligible_endpoint_raises(self, tmp_path: Path) -> None:
        """price_history is not in the stale allow-list, so it raises rather than going stale."""
        flaky = _FlakyProvider()
        client = OneFinanceClient(
            providers=[flaky],
            cache_dir=tmp_path / "cache",
            audit=False,
        )
        client.get_price_history("AAPL", start="2024-01-01", end="2024-01-31")
        flaky.fail = True
        with pytest.raises(AllProvidersFailedError):
            client.get_price_history("AAPL", start="2024-01-01", end="2024-01-31", no_cache=True)
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

    def test_zero_price_falls_through_to_next_provider(self, tmp_path: Path) -> None:
        zero = _ZeroPriceQuoteProvider()
        good = _FakeProvider()
        c = OneFinanceClient(providers=[zero, good], cache_dir=tmp_path / "cache", audit=False)
        q = c.get_quote("VIX3M", no_cache=True)
        assert q.price == 185.64
        assert q.source == "fake"
        c.close()

    def test_zero_price_only_provider_raises(self, tmp_path: Path) -> None:
        zero = _ZeroPriceQuoteProvider()
        c = OneFinanceClient(providers=[zero], cache_dir=tmp_path / "cache", audit=False)
        with pytest.raises(AllProvidersFailedError):
            c.get_quote("VIX3M", no_cache=True)
        c.close()

    def test_zero_price_symbol_is_negative_cached(self, tmp_path: Path) -> None:
        zero = _ZeroPriceQuoteProvider()
        good = _FakeProvider()
        c = OneFinanceClient(providers=[zero, good], cache_dir=tmp_path / "cache", audit=False)
        c.get_quote("VIX3M", no_cache=True)
        assert c.cache.get_negative("zero_price", "quote", "VIX3M") is True
        c.close()


class TestGetQuotes:
    def test_returns_quotes(self, client: OneFinanceClient) -> None:
        quotes = client.get_quotes(["AAPL", "MSFT"], no_cache=True)
        assert len(quotes) == 2
        assert quotes[0].symbol == "AAPL"  # type: ignore[union-attr]
        assert quotes[1].symbol == "MSFT"  # type: ignore[union-attr]

    def test_zero_price_quote_dropped_as_batch_result_missing(self, tmp_path: Path) -> None:
        zero = _ZeroPriceQuoteProvider()
        c = OneFinanceClient(providers=[zero], cache_dir=tmp_path / "cache", audit=False)
        results = c.get_quotes(["VIX3M"], no_cache=True)
        assert len(results) == 1
        assert isinstance(results[0], FinanceError)
        assert results[0].code == "BATCH_RESULT_MISSING"
        c.close()

    def test_partial_cache_hit(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ) -> None:
        # 1. Fetch AAPL (caches AAPL)
        client.get_quote("AAPL")
        assert fake_provider.call_count["quote"] == 1

        # 2. Fetch AAPL and MSFT
        # AAPL should hit cache, MSFT should route to provider
        quotes = client.get_quotes(["AAPL", "MSFT"])

        # Total provider quote calls should be 2: one for AAPL above, one for MSFT here
        assert fake_provider.call_count["quote"] == 2
        assert len(quotes) == 2
        assert quotes[0].symbol == "AAPL"  # type: ignore[union-attr]
        assert quotes[1].symbol == "MSFT"  # type: ignore[union-attr]

    def test_empty_list_returns_empty(self, client: OneFinanceClient) -> None:
        assert client.get_quotes([]) == []

    def test_all_failing_provider_returns_exceptions(self, tmp_path: Path) -> None:
        failing = _FailingProvider()
        c = OneFinanceClient(providers=[failing], cache_dir=tmp_path / "cache", audit=False)
        # Should return list of exceptions
        results = c.get_quotes(["AAPL", "MSFT"], no_cache=True)
        assert len(results) == 2
        assert isinstance(results[0], AllProvidersFailedError)
        assert isinstance(results[1], AllProvidersFailedError)
        c.close()


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


class TestGetEarningsCalendar:
    def test_returns_entries(self, client: OneFinanceClient) -> None:
        results = client.get_earnings_calendar(no_cache=True)
        assert len(results) == 2
        assert all(isinstance(r, EarningsCalendarEntry) for r in results)

    def test_symbol_filter(self, client: OneFinanceClient) -> None:
        results = client.get_earnings_calendar(symbol="AAPL", no_cache=True)
        assert len(results) == 1
        assert results[0].symbol == "AAPL"

    def test_symbol_filter_no_match(self, client: OneFinanceClient) -> None:
        results = client.get_earnings_calendar(symbol="TSLA", no_cache=True)
        assert results == []

    def test_accepts_string_dates(self, client: OneFinanceClient) -> None:
        results = client.get_earnings_calendar(start="2026-07-01", end="2026-07-31", no_cache=True)
        assert len(results) == 2

    def test_accepts_date_objects(self, client: OneFinanceClient) -> None:
        results = client.get_earnings_calendar(
            start=date(2026, 7, 1), end=date(2026, 7, 31), no_cache=True
        )
        assert len(results) == 2

    def test_cache_hit_on_second_call(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ) -> None:
        client.get_earnings_calendar()
        client.get_earnings_calendar()
        assert fake_provider.call_count.get("earnings_calendar", 0) == 1

    def test_no_cache_skips_cache(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ) -> None:
        client.get_earnings_calendar()
        client.get_earnings_calendar(no_cache=True)
        assert fake_provider.call_count.get("earnings_calendar", 0) == 2


class TestCheckProviders:
    def test_returns_report(self, client: OneFinanceClient) -> None:
        report = client.check_providers()
        assert isinstance(report, dict)
        assert "providers" in report


class TestGetShortInterest:
    def test_returns_result_from_cached_fetch(self, client: OneFinanceClient) -> None:
        from unittest.mock import patch

        expected = ShortInterest(
            symbol="AAPL",
            short_interest=50_000_000,
            short_float_pct=2.5,
            days_to_cover=1.2,
            settlement_date=None,
            source="fmp",
            fetched_at=NOW,
        )
        with patch.object(OneFinanceClient, "_cached_fetch", return_value=expected):
            result = client.get_short_interest("AAPL")
        assert isinstance(result, ShortInterest)
        assert result.symbol == "AAPL"
        assert result.short_float_pct == 2.5

    def test_symbol_uppercased(self, client: OneFinanceClient) -> None:
        from unittest.mock import patch

        expected = ShortInterest(symbol="AAPL", source="fmp", fetched_at=NOW)
        with patch.object(OneFinanceClient, "_cached_fetch", return_value=expected) as mock_cf:
            client.get_short_interest("aapl")
        call_kwargs = mock_cf.call_args.kwargs
        assert call_kwargs["symbol"] == "AAPL"


class TestGetMarketSentiment:
    def test_returns_result_from_cached_fetch(self, client: OneFinanceClient) -> None:
        from unittest.mock import patch

        expected = MarketSentiment(
            pcr_equity=0.72,
            pcr_index=1.10,
            pcr_total=0.85,
            as_of_date=None,
            source="fmp",
            fetched_at=NOW,
        )
        with patch.object(OneFinanceClient, "_cached_fetch", return_value=expected):
            result = client.get_market_sentiment()
        assert isinstance(result, MarketSentiment)
        assert result.pcr_total == 0.85

    def test_uses_market_sentiment_endpoint(self, client: OneFinanceClient) -> None:
        from unittest.mock import patch

        expected = MarketSentiment(source="fmp", fetched_at=NOW)
        with patch.object(OneFinanceClient, "_cached_fetch", return_value=expected) as mock_cf:
            client.get_market_sentiment()
        assert mock_cf.call_args.kwargs["endpoint"] == "market_sentiment"


# -----------------------------------------------------------------------
# Price-history range subsumption — serve subranges from a cached superset
# -----------------------------------------------------------------------


class _RangeProvider(BaseProvider):
    """Provider returning one daily bar per calendar day in [start, end]."""

    name = "range"

    def __init__(self) -> None:
        self.calls = 0

    def get_price_history(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> list[PriceBar]:
        self.calls += 1
        bars: list[PriceBar] = []
        d = start
        while d <= end:
            bars.append(
                PriceBar(
                    symbol=symbol,
                    date=d,
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    adj_close=100.5,
                    volume=1_000_000,
                    source=self.name,
                    fetched_at=NOW,
                )
            )
            d = d.fromordinal(d.toordinal() + 1)
        return bars

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 0.0


class TestPriceHistoryRangeSubsumption:
    @pytest.fixture
    def range_provider(self) -> _RangeProvider:
        return _RangeProvider()

    @pytest.fixture
    def range_client(
        self, range_provider: _RangeProvider, tmp_path: Path
    ) -> Generator[OneFinanceClient]:
        c = OneFinanceClient(
            providers=range_provider,
            cache_dir=tmp_path / "range_cache",
            audit=False,
        )
        yield c
        c.close()

    def test_subrange_served_from_cached_superset(
        self, range_client: OneFinanceClient, range_provider: _RangeProvider
    ) -> None:
        wide = range_client.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31))
        assert range_provider.calls == 1
        assert len(wide) == 366  # 2024 is a leap year

        sub = range_client.get_price_history("AAPL", date(2024, 3, 1), date(2024, 3, 31))
        # No new provider call — served by slicing the cached superset
        assert range_provider.calls == 1
        assert len(sub) == 31
        assert all(date(2024, 3, 1) <= b.date <= date(2024, 3, 31) for b in sub)

    def test_non_covering_range_still_fetches(
        self, range_client: OneFinanceClient, range_provider: _RangeProvider
    ) -> None:
        range_client.get_price_history("AAPL", date(2024, 3, 1), date(2024, 3, 31))
        assert range_provider.calls == 1
        # Wider range is NOT covered by the narrow cached entry → must fetch
        range_client.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31))
        assert range_provider.calls == 2

    def test_different_interval_not_reused(
        self, range_client: OneFinanceClient, range_provider: _RangeProvider
    ) -> None:
        range_client.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31), interval="1d")
        assert range_provider.calls == 1
        range_client.get_price_history("AAPL", date(2024, 3, 1), date(2024, 3, 31), interval="1wk")
        assert range_provider.calls == 2

    def test_intraday_interval_not_subsumed(
        self, range_client: OneFinanceClient, range_provider: _RangeProvider
    ) -> None:
        # Intraday providers cap responses, so a cached superset may be
        # incomplete — subsumption must NOT serve subranges for non-daily bars.
        range_client.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31), interval="1wk")
        assert range_provider.calls == 1
        range_client.get_price_history("AAPL", date(2024, 3, 1), date(2024, 3, 31), interval="1wk")
        assert range_provider.calls == 2  # covering subrange still re-fetches

    def test_no_cache_bypasses_subsumption(
        self, range_client: OneFinanceClient, range_provider: _RangeProvider
    ) -> None:
        range_client.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31))
        assert range_provider.calls == 1
        range_client.get_price_history("AAPL", date(2024, 3, 1), date(2024, 3, 31), no_cache=True)
        assert range_provider.calls == 2

    def test_indicators_reuse_cached_history(
        self, range_client: OneFinanceClient, range_provider: _RangeProvider
    ) -> None:
        from datetime import timedelta

        end = date.today()
        start = end - timedelta(days=365)
        range_client.get_price_history("AAPL", start, end)
        assert range_provider.calls == 1
        # Default indicators window (last 180 days) is covered by the year above
        subsumed = range_client.get_indicators("AAPL")
        assert range_provider.calls == 1
        # The subsumed result must equal a direct (uncached) fetch
        fresh = range_client.get_indicators("AAPL", no_cache=True)
        assert range_provider.calls == 2  # no_cache forces a real fetch
        assert subsumed == fresh


# -----------------------------------------------------------------------
# Augment + cache integration
# -----------------------------------------------------------------------


class _ZeroVolumeProvider(BaseProvider):
    """Primary provider that returns a Quote with volume=0 (e.g. Finnhub free tier)."""

    name = "zero_vol"

    def __init__(self) -> None:
        self.call_count = 0

    def get_quote(self, symbol: str) -> Quote:
        self.call_count += 1
        return Quote(
            symbol=symbol,
            timestamp=NOW,
            price=150.0,
            volume=0,
            source=self.name,
            fetched_at=NOW,
        )

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 0.0


class _FullVolumeProvider(BaseProvider):
    """Filler provider that returns a Quote with real volume."""

    name = "full_vol"

    def __init__(self) -> None:
        self.call_count = 0

    def get_quote(self, symbol: str) -> Quote:
        self.call_count += 1
        return Quote(
            symbol=symbol,
            timestamp=NOW,
            price=150.5,
            volume=5_000_000,
            source=self.name,
            fetched_at=NOW,
        )

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 0.0


class TestAugmentCache:
    """Verify that the merged (augmented) result is what gets cached."""

    @pytest.fixture
    def aug_client(
        self,
        tmp_path: Path,
    ) -> Generator[tuple[OneFinanceClient, _ZeroVolumeProvider, _FullVolumeProvider]]:
        primary = _ZeroVolumeProvider()
        filler = _FullVolumeProvider()
        config = OneFinanceConfig(
            tiers={"quote": ["zero_vol", "full_vol"]},
            augment=AugmentConfig(enabled=True, fields={"quote": ["volume", "bid", "ask"]}),
        )
        c = OneFinanceClient(
            providers=[primary, filler],
            config=config,
            cache_dir=tmp_path / "aug_cache",
            audit=False,
        )
        yield c, primary, filler
        c.close()

    def test_augmented_result_is_cached(
        self,
        aug_client: tuple[OneFinanceClient, _ZeroVolumeProvider, _FullVolumeProvider],
    ) -> None:
        """First call: primary returns volume=0, filler augments; merged is cached.
        Second call: cache hit — filler is NOT called again.
        """
        client, primary, filler = aug_client

        # First call — cache miss, augment triggered
        q1 = client.get_quote("AAPL")
        assert q1.volume == 5_000_000
        assert q1.source == "zero_vol+full_vol"
        assert primary.call_count == 1
        assert filler.call_count == 1

        # Second call — cache hit with merged result
        q2 = client.get_quote("AAPL")
        assert q2.volume == 5_000_000
        assert q2.source == "zero_vol+full_vol"
        assert primary.call_count == 1  # not called again
        assert filler.call_count == 1  # not called again (result was cached)


# ---------------------------------------------------------------------------
# fallback_order parameter on OneFinanceClient
# ---------------------------------------------------------------------------


class _NamedFailingProvider(BaseProvider):
    """Provider that always raises ProviderError (configurable name)."""

    def __init__(self, name: str) -> None:
        self.name = name

    def get_quote(self, symbol: str) -> Quote:
        raise ProviderError(
            code="NETWORK_ERROR",
            message=f"{self.name} failed",
            provider=self.name,
            retry_safe=True,
        )

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 60.0


class TestFallbackOrder:
    """Verify fallback_order parameter wires through to the router."""

    def test_fallback_provider_used_when_primary_fails(self, tmp_path: Path) -> None:
        primary = _NamedFailingProvider("primary")
        fallback = _FakeProvider()
        fallback.name = "fallback"

        c = OneFinanceClient(
            providers=[primary, fallback],
            config=OneFinanceConfig(tiers={"quote": ["primary"]}),
            fallback_order=["fallback"],
            cache_dir=tmp_path / "cache",
            audit=False,
        )
        try:
            q = c.get_quote("AAPL")
            assert q.source == "fallback"
        finally:
            c.close()

    def test_empty_fallback_order_no_extra_providers(self, tmp_path: Path) -> None:
        primary = _NamedFailingProvider("primary")
        fallback = _FakeProvider()
        fallback.name = "fallback"

        c = OneFinanceClient(
            providers=[primary, fallback],
            config=OneFinanceConfig(tiers={"quote": ["primary"]}),
            fallback_order=[],
            cache_dir=tmp_path / "cache",
            audit=False,
        )
        try:
            with pytest.raises(AllProvidersFailedError):
                c.get_quote("AAPL")
        finally:
            c.close()

    def test_fallback_order_override_persists_to_config(self, tmp_path: Path) -> None:
        c = OneFinanceClient(
            providers=[_FakeProvider()],
            fallback_order=["yfinance", "fmp"],
            cache_dir=tmp_path / "cache",
            audit=False,
        )
        try:
            assert c._config.fallback_order == ["yfinance", "fmp"]
        finally:
            c.close()
