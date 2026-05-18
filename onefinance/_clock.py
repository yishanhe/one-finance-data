"""Process-wide clock seam.

A single module-level ``Clock`` instance powers all time reads in the
package. Production uses :class:`SystemClock`; tests can swap in a fixed
or scriptable implementation via :func:`set_clock` or :func:`use_clock`.

This module is internal — nothing here is re-exported from
``onefinance.__init__``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Minimal time source. Implementations must be thread-safe."""

    def now(self) -> datetime: ...

    def time(self) -> float: ...

    def perf_counter(self) -> float: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """Default clock — delegates to the standard library."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def time(self) -> float:
        return time.time()

    def perf_counter(self) -> float:
        return time.perf_counter()

    def monotonic(self) -> float:
        return time.monotonic()


_default: Clock = SystemClock()


def get_clock() -> Clock:
    """Return the active process-wide clock."""
    return _default


def set_clock(clock: Clock) -> Clock:
    """Swap the active clock, returning the previous one."""
    global _default
    previous = _default
    _default = clock
    return previous


@contextmanager
def use_clock(clock: Clock) -> Iterator[Clock]:
    """Context manager that temporarily swaps the active clock."""
    previous = set_clock(clock)
    try:
        yield clock
    finally:
        set_clock(previous)
