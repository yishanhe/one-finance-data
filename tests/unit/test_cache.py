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
    ttl_for_option_chain,
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

    def test_last_known_good_round_trip(self, cache: CacheManager) -> None:
        info = _make_info("AAPL")
        cache.set_last_known_good("info:abc", info, ttl=3600, tag="info")
        # Stored under the lkg: prefix, not the bare key.
        assert cache.get("info:abc") is None
        result = cache.get_last_known_good("info:abc")
        assert isinstance(result, CompanyInfo)
        assert result.symbol == "AAPL"


# -----------------------------------------------------------------------
# In-process memo layer (P5)
# -----------------------------------------------------------------------


class TestMemoLayer:
    def test_repeated_get_skips_diskcache_after_first_hit(self, cache: CacheManager) -> None:
        cache.set("info:memo", _make_info("AAPL"), ttl=3600, tag="info")
        with patch.object(cache._cache, "get", wraps=cache._cache.get) as spy:
            first = cache.get("info:memo")
            second = cache.get("info:memo")
        assert isinstance(first, CompanyInfo)
        assert isinstance(second, CompanyInfo)
        # set() already primed the memo, so no diskcache.get() call was needed.
        spy.assert_not_called()

    def test_memo_entry_expires_before_real_ttl(self, cache: CacheManager) -> None:
        with use_clock(FixedClock(instant=NOW, counter=0.0)):
            cache.set("info:short-memo", _make_info("AAPL"), ttl=3600, tag="info")
            assert cache._memo["info:short-memo"][0] == pytest.approx(5.0)  # capped, not 3600

        # Advance past the memo's max TTL (5s) but nowhere near the real 3600s TTL.
        with (
            use_clock(FixedClock(instant=NOW, counter=10.0)),
            patch.object(cache._cache, "get", wraps=cache._cache.get) as spy,
        ):
            result = cache.get("info:short-memo")
        assert isinstance(result, CompanyInfo)
        spy.assert_called_once()  # the stale memo entry was pruned -> fell through to diskcache

    def test_memo_cap_never_exceeds_real_ttl(self, cache: CacheManager) -> None:
        with use_clock(FixedClock(instant=NOW, counter=0.0)):
            cache.set("info:tiny-ttl", _make_info("AAPL"), ttl=2, tag="info")
            expires_at, _ = cache._memo["info:tiny-ttl"]
            assert expires_at <= 2.0  # capped to the real TTL, not the 5s memo max

    def test_invalidate_by_type_clears_memo(self, cache: CacheManager) -> None:
        cache.set("info:a", _make_info("AAPL"), ttl=3600, tag="info")
        assert cache._memo
        cache.invalidate_by_type("info")
        assert not cache._memo

    def test_clear_clears_memo(self, cache: CacheManager) -> None:
        cache.set("info:a", _make_info("AAPL"), ttl=3600, tag="info")
        assert cache._memo
        cache.clear()
        assert not cache._memo

    def test_memo_size_bounded(self, cache: CacheManager) -> None:
        from onefinance.cache.manager import _MEMO_MAX_ENTRIES

        for i in range(_MEMO_MAX_ENTRIES + 50):
            cache.set(f"info:{i}", _make_info("AAPL"), ttl=3600, tag="info")
        assert len(cache._memo) <= _MEMO_MAX_ENTRIES

    def test_zero_ttl_not_memoized(self, cache: CacheManager) -> None:
        cache.set("info:zero", _make_info("AAPL"), ttl=0, tag="info")
        assert "info:zero" not in cache._memo


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

    def test_invalidate_also_clears_last_known_good(self, cache: CacheManager) -> None:
        """Explicit invalidation must evict the stale-on-error copy too."""
        cache.set("info:a", _make_info("AAPL"), ttl=3600, tag="info")
        cache.set_last_known_good("info:a", _make_info("AAPL"), ttl=30 * 86400, tag="info")
        # An unrelated endpoint's LKG must survive.
        cache.set_last_known_good("ratios:z", _make_info("MSFT"), ttl=30 * 86400, tag="ratios")

        cache.invalidate_by_type("info")

        assert cache.get_last_known_good("info:a") is None
        assert cache.get_last_known_good("ratios:z") is not None

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
        assert "hit_rate" in s
        assert s["hit_rate"] == 0.0

    def test_after_inserts(self, cache: CacheManager) -> None:
        cache.set("info:a", _make_info(), ttl=3600, tag="info")
        cache.set("info:b", _make_info("MSFT"), ttl=3600, tag="info")
        s = cache.stats()
        assert s["entries"] == 2
        assert s["size_bytes"] > 0
        assert "hit_rate" in s


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
        assert default_ttl("financials") == 24 * 3600

    def test_info(self) -> None:
        assert default_ttl("info") == 30 * 24 * 3600

    def test_insider_trades(self) -> None:
        assert default_ttl("insider_trades") == 1 * 24 * 3600

    def test_ratios_default(self) -> None:
        assert default_ttl("ratios", fresh=False) == 24 * 3600

    def test_ratios_fresh(self) -> None:
        assert default_ttl("ratios", fresh=True) == 1 * 3600

    def test_earnings_default(self) -> None:
        assert default_ttl("earnings", fresh=False) == 24 * 3600

    def test_earnings_fresh(self) -> None:
        assert default_ttl("earnings", fresh=True) == 1 * 3600


class TestSmartTTLPriceHistory:
    def test_fully_historical(self) -> None:
        """end < today → 30 days."""
        ttl = ttl_for_price_history(date(2020, 1, 1), date(2023, 12, 31))
        assert ttl == 30 * 24 * 3600

    def test_includes_today_market_open_multi_day(self) -> None:
        """end >= today and market open and range > 1 day → 30 min (historical bars settled)."""
        today = date.today()
        with patch("onefinance.cache.manager.is_market_open_now", return_value=True):
            ttl = ttl_for_price_history(date(2024, 1, 1), today)
        assert ttl == 30 * 60

    def test_includes_today_market_open_today_only(self) -> None:
        """end >= today and market open and range == 0 days (today only) → 1 min."""
        today = date.today()
        with patch("onefinance.cache.manager.is_market_open_now", return_value=True):
            ttl = ttl_for_price_history(today, today)
        assert ttl == 60

    def test_includes_today_market_closed(self) -> None:
        """end >= today and market closed → 6 hours."""
        today = date.today()
        with patch("onefinance.cache.manager.is_market_open_now", return_value=False):
            ttl = ttl_for_price_history(date(2024, 1, 1), today)
        assert ttl == 6 * 3600


class TestOptionChainTTL:
    def test_market_open_returns_5_min(self) -> None:
        with patch("onefinance.cache.manager.is_market_open_now", return_value=True):
            assert ttl_for_option_chain() == 5 * 60

    def test_market_closed_returns_4_hours(self) -> None:
        with patch("onefinance.cache.manager.is_market_open_now", return_value=False):
            assert ttl_for_option_chain() == 4 * 3600


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


class TestPriceRangeSubsumption:
    """Direct CacheManager coverage for the price-range index."""

    def _store_range(
        self, cache: CacheManager, start: date, end: date, *, interval: str = "1d"
    ) -> str:
        key = cache.make_key(
            "price_history", symbol="AAPL", start=start, end=end, interval=interval
        )
        d = start
        bars: list[PriceBar] = []
        while d <= end:
            bars.append(_make_bar(d=d))
            d = d.fromordinal(d.toordinal() + 1)
        cache.set(key, bars, ttl=3600, tag="price_history")
        cache.record_price_range("AAPL", interval, start, end, key)
        return key

    def test_covering_range_slices_inclusive(self, cache: CacheManager) -> None:
        self._store_range(cache, date(2024, 1, 1), date(2024, 1, 31))
        out = cache.find_covering_price_range("AAPL", "1d", date(2024, 1, 10), date(2024, 1, 20))
        assert out is not None
        dates = [b.date for b in out]
        assert dates[0] == date(2024, 1, 10)  # lower bound inclusive
        assert dates[-1] == date(2024, 1, 20)  # upper bound inclusive
        assert len(out) == 11

    def test_exact_range_returns_all_bars(self, cache: CacheManager) -> None:
        self._store_range(cache, date(2024, 1, 1), date(2024, 1, 31))
        out = cache.find_covering_price_range("AAPL", "1d", date(2024, 1, 1), date(2024, 1, 31))
        assert out is not None
        assert len(out) == 31

    def test_non_covering_returns_none(self, cache: CacheManager) -> None:
        self._store_range(cache, date(2024, 1, 10), date(2024, 1, 20))
        # Requested range extends beyond the cached one on both ends
        assert (
            cache.find_covering_price_range("AAPL", "1d", date(2024, 1, 1), date(2024, 1, 31))
            is None
        )

    def test_different_interval_returns_none(self, cache: CacheManager) -> None:
        self._store_range(cache, date(2024, 1, 1), date(2024, 1, 31), interval="1d")
        assert (
            cache.find_covering_price_range("AAPL", "1wk", date(2024, 1, 10), date(2024, 1, 20))
            is None
        )

    def test_evicted_superset_falls_through(self, cache: CacheManager) -> None:
        key = self._store_range(cache, date(2024, 1, 1), date(2024, 1, 31))
        cache._cache.delete(key)  # superset gone, index still points at it
        cache._memo.clear()  # simulate real eviction: the in-process memo wouldn't know either
        assert (
            cache.find_covering_price_range("AAPL", "1d", date(2024, 1, 10), date(2024, 1, 20))
            is None
        )


# ---------------------------------------------------------------------------
# Delta-fetch: find_extendable_price_range + extend_price_range
# ---------------------------------------------------------------------------


class TestDeltaFetch:
    """Tests for the P1-step-2 delta-fetch helpers."""

    def _store_range(
        self,
        cache: CacheManager,
        start: date,
        end: date,
        symbol: str = "AAPL",
        interval: str = "1d",
    ) -> str:
        key = cache.make_key(
            "price_history", symbol=symbol, start=start, end=end, interval=interval
        )
        d = start
        bars: list[PriceBar] = []
        while d <= end:
            bars.append(_make_bar(symbol=symbol, d=d))
            d = d.fromordinal(d.toordinal() + 1)
        cache.set(key, bars, ttl=3600, tag="price_history")
        cache.record_price_range(symbol, interval, start, end, key)
        return key

    def test_finds_extendable_range(self, cache: CacheManager) -> None:
        # Cache [2024-01-01, 2024-01-10]; request [2024-01-01, 2024-01-15]
        # Mon Jan 15 2024 is a holiday (MLK Day) but Jan 11-14 are trading days
        self._store_range(cache, date(2024, 1, 1), date(2024, 1, 10))
        result = cache.find_extendable_price_range(
            "AAPL", "1d", date(2024, 1, 1), date(2024, 1, 15)
        )
        assert result is not None
        bars, e_end, e_key = result
        assert e_end == date(2024, 1, 10)
        assert len(bars) == 10

    def test_no_extension_when_gap_has_no_trading_days(self, cache: CacheManager) -> None:
        # Cache [2024-01-01, 2024-01-05 (Friday)]; request [2024-01-01, 2024-01-07 (Sunday)]
        # Gap is Sat+Sun — no trading days, so covering check should handle it, not delta
        self._store_range(cache, date(2024, 1, 1), date(2024, 1, 5))
        result = cache.find_extendable_price_range("AAPL", "1d", date(2024, 1, 1), date(2024, 1, 7))
        assert result is None  # no trading days in gap

    def test_no_extension_when_fully_covered(self, cache: CacheManager) -> None:
        self._store_range(cache, date(2024, 1, 1), date(2024, 1, 31))
        result = cache.find_extendable_price_range(
            "AAPL", "1d", date(2024, 1, 1), date(2024, 1, 20)
        )
        assert result is None  # already fully covered

    def test_no_extension_when_different_start(self, cache: CacheManager) -> None:
        self._store_range(cache, date(2024, 1, 5), date(2024, 1, 20))
        result = cache.find_extendable_price_range(
            "AAPL", "1d", date(2024, 1, 1), date(2024, 1, 25)
        )
        assert result is None  # different start — delta-fetch can't reconstruct [1,4]

    def test_extend_price_range_merges_bars(self, cache: CacheManager) -> None:
        # Store [Jan 1–5], then extend with [Jan 8–10]
        original_end = date(2024, 1, 5)
        key = self._store_range(cache, date(2024, 1, 1), original_end)

        new_bars = [
            _make_bar(d=date(2024, 1, 8)),
            _make_bar(d=date(2024, 1, 9)),
            _make_bar(d=date(2024, 1, 10)),
        ]
        original_bars = cache.get(key)
        assert original_bars is not None
        all_bars = cast(list[PriceBar], list(original_bars)) + new_bars
        all_bars.sort(key=lambda b: b.date)

        cache.extend_price_range(
            "AAPL",
            "1d",
            original_start=date(2024, 1, 1),
            original_end=original_end,
            new_end=date(2024, 1, 10),
            original_key=key,
            all_bars=all_bars,
            ttl=3600,
        )

        # The key now holds 8 bars (5 + 3)
        updated = cache.get(key)
        assert isinstance(updated, list)
        assert len(updated) == 8

        # And find_covering_price_range now serves the extended range
        covered = cache.find_covering_price_range("AAPL", "1d", date(2024, 1, 1), date(2024, 1, 10))
        assert covered is not None
        assert len(covered) == 8


# ---------------------------------------------------------------------------
# NYSE holiday table and holiday-aware helpers
# ---------------------------------------------------------------------------


class TestNYSEHolidays:
    """Test that NYSE holidays are recognised correctly."""

    def test_independence_day_2025_is_holiday(self) -> None:
        from onefinance.cache.manager import _NYSE_HOLIDAYS, _is_trading_day

        assert date(2025, 7, 4) in _NYSE_HOLIDAYS
        assert not _is_trading_day(date(2025, 7, 4))

    def test_regular_weekday_is_trading_day(self) -> None:
        from onefinance.cache.manager import _is_trading_day

        # July 3 2025 is a Thursday (regular trading day)
        assert _is_trading_day(date(2025, 7, 3))

    def test_weekend_is_not_trading_day(self) -> None:
        from onefinance.cache.manager import _is_trading_day

        assert not _is_trading_day(date(2025, 7, 5))  # Saturday

    def test_holiday_gap_not_trading(self) -> None:
        from onefinance.cache.manager import _has_trading_days_in_gap

        # Gap = [Jul 4 2025] — holiday only, no trading day
        assert not _has_trading_days_in_gap(date(2025, 7, 3), date(2025, 7, 4))

    def test_holiday_weekend_gap_has_trading_days(self) -> None:
        from onefinance.cache.manager import _has_trading_days_in_gap

        # Jul 4 (holiday) + Jul 7 (Monday) → Jul 7 is a trading day
        assert _has_trading_days_in_gap(date(2025, 7, 3), date(2025, 7, 7))

    def test_is_market_open_returns_false_on_holiday(self) -> None:
        # Christmas 2025 is a Thursday — should be closed
        xmas_et = datetime(
            2025, 12, 25, 11, 0, 0, tzinfo=UTC
        )  # 6am ET — market would be open if not holiday
        with use_clock(FixedClock(instant=xmas_et)):
            assert not is_market_open_now()


# ---------------------------------------------------------------------------
# Global negative cache (P4)
# ---------------------------------------------------------------------------


class TestGlobalNegativeCache:
    def test_set_and_get_global_negative(self, cache: CacheManager) -> None:
        assert not cache.get_negative_global("fmp", "short_interest")
        cache.set_negative_global("fmp", "short_interest", ttl=60)
        assert cache.get_negative_global("fmp", "short_interest")

    def test_global_negative_does_not_affect_other_endpoints(self, cache: CacheManager) -> None:
        cache.set_negative_global("fmp", "short_interest", ttl=60)
        assert not cache.get_negative_global("fmp", "quote")

    def test_global_negative_does_not_affect_other_providers(self, cache: CacheManager) -> None:
        cache.set_negative_global("fmp", "short_interest", ttl=60)
        assert not cache.get_negative_global("finnhub", "short_interest")


# ---------------------------------------------------------------------------
# Endpoint-ok marker (recent-success evidence for the global-bench veto)
# ---------------------------------------------------------------------------


class TestEndpointOkMarker:
    def test_unmarked_endpoint_is_not_ok(self, cache: CacheManager) -> None:
        assert not cache.get_endpoint_ok("finnhub", "quote")

    def test_mark_and_get(self, cache: CacheManager) -> None:
        cache.mark_endpoint_ok("finnhub", "quote")
        assert cache.get_endpoint_ok("finnhub", "quote")
        assert not cache.get_endpoint_ok("finnhub", "ratios")
        assert not cache.get_endpoint_ok("fmp", "quote")

    def test_mark_heals_existing_global_bench(self, cache: CacheManager) -> None:
        cache.set_negative_global("finnhub", "quote", ttl=60)
        assert cache.get_negative_global("finnhub", "quote")
        cache.mark_endpoint_ok("finnhub", "quote")
        assert not cache.get_negative_global("finnhub", "quote")

    def test_mark_does_not_clear_per_symbol_negative(self, cache: CacheManager) -> None:
        cache.set_negative("finnhub", "quote", "000660.KS", ttl=60)
        cache.mark_endpoint_ok("finnhub", "quote")
        assert cache.get_negative("finnhub", "quote", "000660.KS")


# ---------------------------------------------------------------------------
# Augment filler cache (P2-A)
# ---------------------------------------------------------------------------


class TestAugmentCache:
    def test_set_and_get_augment(self, cache: CacheManager) -> None:
        bar = _make_bar()
        cache.set_augment("quote", "AAPL", bar, ttl=300)
        result = cache.get_augment("quote", "AAPL")
        assert result is not None

    def test_augment_miss_returns_none(self, cache: CacheManager) -> None:
        assert cache.get_augment("quote", "TSLA") is None

    def test_augment_is_case_insensitive_on_symbol(self, cache: CacheManager) -> None:
        bar = _make_bar()
        cache.set_augment("quote", "aapl", bar, ttl=300)
        assert cache.get_augment("quote", "AAPL") is not None


# ---------------------------------------------------------------------------
# Router state persistence (P3)
# ---------------------------------------------------------------------------


class TestRouterState:
    def test_set_and_get_router_state(self, cache: CacheManager) -> None:
        state = {"cooldown_until": 9999.0, "consecutive_failures": 3, "last_error": "HTTP 429"}
        cache.set_router_state("fmp", state, ttl=300)
        loaded = cache.get_router_state("fmp")
        assert loaded is not None
        assert loaded["consecutive_failures"] == 3
        assert loaded["last_error"] == "HTTP 429"

    def test_missing_provider_state_returns_none(self, cache: CacheManager) -> None:
        assert cache.get_router_state("nonexistent_provider") is None


# ---------------------------------------------------------------------------
# list_global_negatives — plan-gated pairs for `providers check`
# ---------------------------------------------------------------------------


class TestListGlobalNegatives:
    def test_empty_when_nothing_benched(self, cache: CacheManager) -> None:
        assert cache.list_global_negatives() == []

    def test_lists_only_global_entries(self, cache: CacheManager) -> None:
        cache.set_negative_global("fmp", "ratios", ttl=60)
        cache.set_negative_global("finnhub", "quote", ttl=60)
        # Per-symbol entries must not appear.
        cache.set_negative("yfinance", "quote", "VIX", ttl=60)

        assert cache.list_global_negatives() == [
            ("finnhub", "quote"),
            ("fmp", "ratios"),
        ]

    def test_healed_bench_disappears(self, cache: CacheManager) -> None:
        cache.set_negative_global("finnhub", "quote", ttl=60)
        cache.mark_endpoint_ok("finnhub", "quote")
        assert cache.list_global_negatives() == []
