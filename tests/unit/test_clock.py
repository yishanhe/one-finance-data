"""Tests for the process-wide clock seam."""

from __future__ import annotations

from datetime import UTC, datetime

from onefinance._clock import Clock, SystemClock, get_clock, set_clock, use_clock


class FixedClock:
    """Frozen-time stub used across tests."""

    def __init__(self, *, instant: datetime, counter: float = 0.0, mono: float = 0.0) -> None:
        self._instant = instant
        self._counter = counter
        self._mono = mono

    def now(self) -> datetime:
        return self._instant

    def time(self) -> float:
        return self._instant.timestamp()

    def perf_counter(self) -> float:
        return self._counter

    def monotonic(self) -> float:
        return self._mono


def test_system_clock_returns_utc_aware_datetime() -> None:
    sc = SystemClock()
    assert sc.now().tzinfo is UTC


def test_system_clock_satisfies_protocol() -> None:
    sc: Clock = SystemClock()
    assert isinstance(sc.now(), datetime)
    assert isinstance(sc.time(), float)
    assert isinstance(sc.perf_counter(), float)
    assert isinstance(sc.monotonic(), float)


def test_set_clock_returns_previous_and_swaps() -> None:
    sc = SystemClock()
    fixed = FixedClock(instant=datetime(2030, 1, 1, tzinfo=UTC))
    previous = set_clock(fixed)
    try:
        assert get_clock() is fixed
        assert get_clock().now() == datetime(2030, 1, 1, tzinfo=UTC)
    finally:
        set_clock(previous)
    assert isinstance(get_clock(), SystemClock) or get_clock() is sc


def test_use_clock_restores_previous_after_block() -> None:
    fixed = FixedClock(instant=datetime(2030, 6, 1, tzinfo=UTC))
    baseline = get_clock()
    with use_clock(fixed):
        assert get_clock() is fixed
    assert get_clock() is baseline


def test_use_clock_restores_even_on_exception() -> None:
    fixed = FixedClock(instant=datetime(2030, 6, 1, tzinfo=UTC))
    baseline = get_clock()
    try:
        with use_clock(fixed):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert get_clock() is baseline
