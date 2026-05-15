"""Unit tests for OneFinanceClient."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from onefinance.core.client import OneFinanceClient
from onefinance.core.errors import (
    AllProvidersFailedError,
    InvalidArgumentError,
    NotSupportedError,
    ProviderError,
)
from onefinance.core.models import CompanyInfo, PriceBar
from onefinance.providers.base import BaseProvider

NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)


# -----------------------------------------------------------------------
# Test provider stubs
# -----------------------------------------------------------------------

class _FakeProvider(BaseProvider):
    """Provider that returns canned data for price_history and info."""

    name = "fake"

    def __init__(self) -> None:
        self.call_count: dict[str, int] = {}

    def get_price_history(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> list[PriceBar]:
        self.call_count["price_history"] = self.call_count.get("price_history", 0) + 1
        return [
            PriceBar(
                symbol=symbol,
                date=date(2024, 1, 2),
                open=185.0, high=186.5, low=184.0, close=185.64,
                adj_close=185.64, volume=50_000_000,
                source=self.name, fetched_at=NOW,
            )
        ]

    def get_info(self, symbol: str) -> CompanyInfo:
        self.call_count["info"] = self.call_count.get("info", 0) + 1
        return CompanyInfo(
            symbol=symbol, name=f"{symbol} Inc.",
            source=self.name, fetched_at=NOW,
        )

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
        raise ProviderError(
            "NETWORK_ERROR", "network down", provider=self.name, retry_safe=True
        )

    def get_info(self, symbol: str) -> CompanyInfo:
        raise ProviderError(
            "NETWORK_ERROR", "network down", provider=self.name, retry_safe=True
        )

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 0.0


@pytest.fixture
def fake_provider() -> _FakeProvider:
    return _FakeProvider()


@pytest.fixture
def client(fake_provider: _FakeProvider, tmp_path: Path) -> OneFinanceClient:
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
    def test_returns_bars(self, client: OneFinanceClient):
        bars = client.get_price_history("AAPL", "2024-01-01", "2024-12-31")
        assert len(bars) == 1
        assert bars[0].symbol == "AAPL"
        assert bars[0].source == "fake"

    def test_date_objects_work(self, client: OneFinanceClient):
        bars = client.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31))
        assert len(bars) == 1

    def test_end_defaults_to_today(self, client: OneFinanceClient):
        bars = client.get_price_history("AAPL", "2024-01-01")
        assert len(bars) == 1

    def test_invalid_date_range(self, client: OneFinanceClient):
        with pytest.raises(InvalidArgumentError):
            client.get_price_history("AAPL", "2024-12-31", "2024-01-01")


class TestGetInfo:
    def test_returns_info(self, client: OneFinanceClient):
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
    ):
        """Second call should hit cache, not the provider."""
        client.get_info("AAPL")
        client.get_info("AAPL")
        assert fake_provider.call_count["info"] == 1  # only one provider call

    def test_no_cache_bypasses(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ):
        """no_cache=True should always hit the provider."""
        client.get_info("AAPL", no_cache=True)
        client.get_info("AAPL", no_cache=True)
        assert fake_provider.call_count["info"] == 2  # two provider calls

    def test_no_cache_does_not_write(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ):
        """no_cache=True should not write to cache either."""
        client.get_info("AAPL", no_cache=True)
        # Now fetch normally — should call provider since nothing was cached
        client.get_info("AAPL")
        assert fake_provider.call_count["info"] == 2

    def test_different_symbols_different_cache(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ):
        client.get_info("AAPL")
        client.get_info("MSFT")
        assert fake_provider.call_count["info"] == 2

    def test_cache_stats_accessible(self, client: OneFinanceClient):
        client.get_info("AAPL")
        stats = client.cache.stats()
        assert stats["entries"] >= 1

    def test_cache_invalidation(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ):
        """After invalidation, provider is called again."""
        client.get_info("AAPL")
        client.cache.invalidate_by_type("info")
        client.get_info("AAPL")
        assert fake_provider.call_count["info"] == 2


# -----------------------------------------------------------------------
# Provider selection
# -----------------------------------------------------------------------

class TestProviderSelection:
    def test_force_provider(self, tmp_path: Path):
        p1 = _FakeProvider()
        p1.name = "primary"
        p2 = _FakeProvider()
        p2.name = "secondary"
        client = OneFinanceClient(
            providers=[p1, p2], cache_dir=tmp_path / "cache",
            audit=False,
        )
        info = client.get_info("AAPL", provider="secondary", no_cache=True)
        assert info.source == "secondary"
        assert p2.call_count.get("info") == 1
        assert p1.call_count.get("info", 0) == 0
        client.close()

    def test_unknown_provider_raises(self, client: OneFinanceClient):
        with pytest.raises(InvalidArgumentError):
            client.get_info("AAPL", provider="nonexistent")


# -----------------------------------------------------------------------
# Provider fallback
# -----------------------------------------------------------------------

class TestProviderFallback:
    def test_falls_through_on_not_supported(self, tmp_path: Path):
        """If first provider doesn't support endpoint, try next."""
        # _MinimalProvider from test_base_provider doesn't override get_info
        class _MinimalProv(BaseProvider):
            name = "minimal"
            def is_rate_limited(self, r: Any) -> bool: return False
            def cooldown_for(self, r: Any) -> float: return 0.0

        minimal = _MinimalProv()
        fake = _FakeProvider()
        client = OneFinanceClient(
            providers=[minimal, fake], cache_dir=tmp_path / "cache",
            audit=False,
        )
        info = client.get_info("AAPL", no_cache=True)
        assert info.source == "fake"
        client.close()

    def test_all_providers_fail_raises(self, tmp_path: Path):
        failing = _FailingProvider()
        client = OneFinanceClient(
            providers=[failing], cache_dir=tmp_path / "cache",
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
    def test_custom_ttl(
        self, client: OneFinanceClient, fake_provider: _FakeProvider
    ):
        """Custom TTL should be passed through to cache."""
        # Just verify it doesn't error — TTL correctness is tested in cache tests
        client.get_info("AAPL", ttl=60)
        assert fake_provider.call_count["info"] == 1


# -----------------------------------------------------------------------
# Context manager
# -----------------------------------------------------------------------

class TestContextManager:
    def test_with_statement(self, tmp_path: Path):
        with OneFinanceClient(
            providers=_FakeProvider(),
            cache_dir=tmp_path / "cache",
            audit=False,
        ) as client:
            info = client.get_info("AAPL")
            assert info.symbol == "AAPL"
