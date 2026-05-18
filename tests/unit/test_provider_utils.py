"""Tests for shared provider helpers in onefinance.providers._utils."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from onefinance.providers._utils import (
    _safe_float,
    _safe_int,
    format_period,
    normalize_symbol,
    parse_iso_date,
    parse_iso_datetime_utc,
    quarter_from_date,
    utc_now,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aapl", "AAPL"),
        ("AAPL", "AAPL"),
        ("  aapl  ", "AAPL"),
        ("brk.b", "BRK.B"),
    ],
)
def test_normalize_symbol(raw: str, expected: str) -> None:
    assert normalize_symbol(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-01-15", date(2024, 1, 15)),
        ("2024-01-15 10:30:00", date(2024, 1, 15)),
        ("2024-12-31T23:59:59Z", date(2024, 12, 31)),
    ],
)
def test_parse_iso_date(raw: str, expected: date) -> None:
    assert parse_iso_date(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-01-15T10:30:00Z", datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)),
        ("2024-01-15T10:30:00+00:00", datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)),
        ("2024-01-15 10:30:00", datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)),
        ("2024-01-15T10:30:00+05:00", datetime(2024, 1, 15, 5, 30, 0, tzinfo=UTC)),
    ],
)
def test_parse_iso_datetime_utc(raw: str, expected: datetime) -> None:
    parsed = parse_iso_datetime_utc(raw)
    assert parsed == expected
    assert parsed.tzinfo is UTC


@pytest.mark.parametrize(
    ("month", "expected_quarter"),
    [
        (1, 1),
        (2, 1),
        (3, 1),
        (4, 2),
        (5, 2),
        (6, 2),
        (7, 3),
        (8, 3),
        (9, 3),
        (10, 4),
        (11, 4),
        (12, 4),
    ],
)
def test_quarter_from_date(month: int, expected_quarter: int) -> None:
    assert quarter_from_date(date(2024, month, 15)) == expected_quarter


@pytest.mark.parametrize(
    ("year", "quarter", "expected"),
    [
        (2024, None, "2024"),
        (2024, "", "2024"),
        (2024, 1, "2024-Q1"),
        (2024, 3, "2024-Q3"),
        (2024, "Q2", "2024-Q2"),
        (2024, "FY", "2024-FY"),
        ("2024", "FY", "2024-FY"),
    ],
)
def test_format_period(year: int | str, quarter: int | str | None, expected: str) -> None:
    assert format_period(year, quarter) == expected


def test_utc_now_returns_utc_aware() -> None:
    now = utc_now()
    assert now.tzinfo is UTC


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("3.14", 3.14),
        (3, 3.0),
        ("nope", None),
    ],
)
def test_safe_float(raw: object, expected: float | None) -> None:
    assert _safe_float(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("3", 3),
        (3.7, 3),
        ("nope", None),
    ],
)
def test_safe_int(raw: object, expected: int | None) -> None:
    assert _safe_int(raw) == expected
