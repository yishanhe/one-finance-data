"""Unit tests for CacheManager."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from onefinance._clock import use_clock
from onefinance.cache.manager import (
    CacheManager,
    _deserialise_envelope,
    _serialise_envelope,
    default_ttl,
    is_market_open_now,
    ttl_for_price_history,
)
from onefinance.core.models import CompanyInfo, FinanceModel, PriceBar
from tests.unit.test_clock import FixedClock

NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def cache(tmp_path: Path) -> Generator[CacheManager]:
    """Create a CacheManager backed by a temp directory."""
    mgr = CacheManager(cache_dir=tmp_path / "test_cache", size_limit_gb=0.01)
    yield mgr
    mgr.close()


def _make_bar(symbol: str = "AAPL", d: date = date(2024, 1, 2)) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        date=d,
        open=185.0,
        high=186.5,
        low=184.0,
        close=185.64,
        adj_close=185.64,
        volume=50_000_000,
        source="yfinance",
        fetched_at=NOW,
    )


def _make_info(symbol: str = "AAPL") -> CompanyInfo:
    return CompanyInfo(
        symbol=symbol,
        name="Apple Inc.",
        sector="Technology",
        source="yfinance",
        fetched_at=NOW,
    )


# -----------------------------------------------------------------------
# Serialisation round-trip
# -----------------------------------------------------------------------


class TestSerialisation:
    def test_single_model_round_trip(self) -> None:
        info = _make_info()
        envelope = _serialise_envelope(info)
        assert envelope["type"] == "CompanyInfo"
        assert envelope["is_list"] is False

        result = _deserialise_envelope(envelope)
        assert isinstance(result, CompanyInfo)
        assert result.symbol == "AAPL"
        assert result.name == "Apple Inc."

    def test_list_model_round_trip(self) -> None:
        bars = [_make_bar("AAPL", date(2024, 1, 2)), _make_bar("AAPL", date(2024, 1, 3))]
        envelope = _serialise_envelope(cast(list[FinanceModel], bars))
        assert envelope["type"] == "PriceBar"
        assert envelope["is_list"] is True
        assert len(envelope["data"]) == 2

        result = _deserialise_envelope(envelope)
        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(b, PriceBar) for b in result)

    def test_empty_list(self) -> None:
        envelope = _serialise_envelope([])
        assert envelope["type"] == "empty"
        result = _deserialise_envelope(envelope)
        assert result == []

    def test_unknown_type_returns_none(self) -> None:
        envelope = {"type": "UnknownModel", "is_list": False, "data": {}}
        result = _deserialise_envelope(envelope)
        assert result is None

    def test_date_list_round_trip(self) -> None:
        """list[date] (e.g. options expirations) must survive cache serialisation."""
        dates = [date(2026, 6, 19), date(2026, 7, 17), date(2026, 9, 18)]
        envelope = _serialise_envelope(dates)
        assert envelope["type"] == "__date_list__"
        assert envelope["is_list"] is True
        assert envelope["data"] == ["2026-06-19", "2026-07-17", "2026-09-18"]

        result = _deserialise_envelope(envelope)
        assert result == dates

    def test_date_list_cache_roundtrip(self, cache: CacheManager) -> None:
        """End-to-end: set list[date] → get back list[date] from cache."""
        dates = [date(2026, 6, 19), date(2026, 7, 17)]
        cache.set("options_expirations:MU", dates, ttl=3600, tag="options_expirations")
        result = cache.get("options_expirations:MU")
        assert result == dates


# -----------------------------------------------------------------------
# CacheManager get/set
# -----------------------------------------------------------------------


class TestCacheManagerGetSet:
    def test_miss_returns_none(self, cache: CacheManager) -> None:
        assert cache.get("nonexistent:key") is None

    def test_set_and_get_single(self, cache: CacheManager) -> None:
        info = _make_info()
        cache.set("info:abc123", info, ttl=3600, tag="info")
        result = cache.get("info:abc123")
        assert isinstance(result, CompanyInfo)
        assert result.symbol == "AAPL"

    def test_set_and_get_list(self, cache: CacheManager) -> None:
        bars = [_make_bar("AAPL", date(2024, 1, 2)), _make_bar("AAPL", date(2024, 1, 3))]
        cache.set(
            "price_history:xyz789",
            cast(list[FinanceModel], bars),
            ttl=3600,
            tag="price_history",
        )
        result = cache.get("price_history:xyz789")
        assert isinstance(result, list)
        assert len(result) == 2

    def test_expired_entry_returns_none(self, cache: CacheManager) -> None:
        info = _make_info()
        cache.set("info:expire_test", info, ttl=0, tag="info")  # TTL=0 means expired immediately
        # diskcache may still return it right away; sleep not reliable in unit tests
        # So we test with a very small TTL and clear expired
        import time

        time.sleep(0.1)
        cache._cache.expire()  # Force expiration sweep
        result = cache.get("info:expire_test")
        assert result is None

    def test_set_empty_list(self, cache: CacheManager) -> None:
        cache.set("price_history:empty", [], ttl=3600, tag="price_history")
        result = cache.get("price_history:empty")
        assert result == []


# -----------------------------------------------------------------------
# Tag-based invalidation
# -----------------------------------------------------------------------


class TestInvalidation:
    def test_invalidate_by_type(self, cache: CacheManager) -> None:
        cache.set("info:a", _make_info("AAPL"), ttl=3600, tag="info")
        cache.set("info:b", _make_info("MSFT"), ttl=3600, tag="info")
        cache.set(
            "price_history:c",
            cast(list[FinanceModel], [_make_bar()]),
            ttl=3600,
            tag="price_history",
        )

        cache.invalidate_by_type("info")

        assert cache.get("info:a") is None
        assert cache.get("info:b") is None
        # Price history should be untouched
        assert cache.get("price_history:c") is not None

    def test_clear(self, cache: CacheManager) -> None:
        cache.set("info:a", _make_info(), ttl=3600, tag="info")
        cache.set(
            "price_history:b",
            cast(list[FinanceModel], [_make_bar()]),
            ttl=3600,
            tag="price_history",
        )
        cache.clear()
        assert cache.get("info:a") is None
        assert cache.get("price_history:b") is None


# -----------------------------------------------------------------------
# Stats
# -----------------------------------------------------------------------


class TestStats:
    def test_empty_cache(self, cache: CacheManager) -> None:
        s = cache.stats()
        assert s["entries"] == 0
        assert s["size_bytes"] >= 0

    def test_after_inserts(self, cache: CacheManager) -> None:
        cache.set("info:a", _make_info(), ttl=3600, tag="info")
        cache.set("info:b", _make_info("MSFT"), ttl=3600, tag="info")
        s = cache.stats()
        assert s["entries"] == 2
        assert s["size_bytes"] > 0


# -----------------------------------------------------------------------
# make_key convenience
# -----------------------------------------------------------------------


class TestMakeKeyProxy:
    def test_proxy(self, cache: CacheManager) -> None:
        key = cache.make_key("price_history", symbol="AAPL", start="2024-01-01")
        assert key.startswith("price_history:")


# -----------------------------------------------------------------------
# TTL logic
# -----------------------------------------------------------------------


class TestDefaultTTL:
    def test_quote(self) -> None:
        assert default_ttl("quote") == 30

    def test_financials(self) -> None:
        assert default_ttl("financials") == 7 * 24 * 3600

    def test_info(self) -> None:
        assert default_ttl("info") == 30 * 24 * 3600

    def test_insider_trades(self) -> None:
        assert default_ttl("insider_trades") == 1 * 24 * 3600

    def test_ratios_default(self) -> None:
        assert default_ttl("ratios", fresh=False) == 7 * 24 * 3600

    def test_ratios_fresh(self) -> None:
        assert default_ttl("ratios", fresh=True) == 1 * 3600

    def test_earnings_default(self) -> None:
        assert default_ttl("earnings", fresh=False) == 7 * 24 * 3600

    def test_earnings_fresh(self) -> None:
        assert default_ttl("earnings", fresh=True) == 1 * 3600


class TestSmartTTLPriceHistory:
    def test_fully_historical(self) -> None:
        """end < today → 30 days."""
        ttl = ttl_for_price_history(date(2020, 1, 1), date(2023, 12, 31))
        assert ttl == 30 * 24 * 3600

    def test_includes_today_market_open(self) -> None:
        """end >= today and market open → 1 minute."""
        today = date.today()
        with patch("onefinance.cache.manager.is_market_open_now", return_value=True):
            ttl = ttl_for_price_history(date(2024, 1, 1), today)
        assert ttl == 60

    def test_includes_today_market_closed(self) -> None:
        """end >= today and market closed → 6 hours."""
        today = date.today()
        with patch("onefinance.cache.manager.is_market_open_now", return_value=False):
            ttl = ttl_for_price_history(date(2024, 1, 1), today)
        assert ttl == 6 * 3600


class TestMarketOpen:
    def test_weekday_during_hours(self) -> None:
        """Wednesday 14:00 UTC = 10:00 ET → open."""
        instant = datetime(2026, 5, 13, 14, 0, 0, tzinfo=UTC)
        with use_clock(FixedClock(instant=instant)):
            assert is_market_open_now() is True

    def test_weekend(self) -> None:
        """Saturday at any time → closed."""
        instant = datetime(2026, 5, 16, 14, 0, 0, tzinfo=UTC)
        with use_clock(FixedClock(instant=instant)):
            assert is_market_open_now() is False

    def test_weekday_before_open(self) -> None:
        """Wednesday 12:00 UTC = 08:00 ET → closed."""
        instant = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        with use_clock(FixedClock(instant=instant)):
            assert is_market_open_now() is False

    def test_returns_bool(self) -> None:
        """Smoke test: is_market_open_now always returns bool."""
        assert isinstance(is_market_open_now(), bool)
