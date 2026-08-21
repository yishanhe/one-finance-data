"""Unit tests for onefinance.pandas DataFrame converters."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd  # type: ignore[import-untyped]

from onefinance.core.models import (
    IncomeStatement,
    OptionChain,
    OptionContract,
    PriceBar,
    Quote,
)
from onefinance.indicators.core import TechnicalIndicators
from onefinance.pandas import (
    bars_to_df,
    financials_to_df,
    indicators_to_df,
    options_to_df,
    quotes_to_df,
    to_df,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_bars_to_df() -> None:
    bars = [
        PriceBar(
            symbol="AAPL",
            date=date(2024, 1, 2),
            open=100.0,
            high=105.0,
            low=99.0,
            close=104.0,
            adj_close=104.0,
            volume=1000,
            source="test",
            fetched_at=NOW,
        ),
        PriceBar(
            symbol="AAPL",
            date=date(2024, 1, 3),
            open=104.0,
            high=106.0,
            low=103.0,
            close=105.0,
            adj_close=105.0,
            volume=1200,
            source="test",
            fetched_at=NOW,
        ),
    ]
    df = bars_to_df(bars)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert df.index.name == "date"
    assert "close" in df.columns
    assert df.loc[date(2024, 1, 2), "close"] == 104.0


def test_quotes_to_df() -> None:
    quote = Quote(
        symbol="AAPL",
        timestamp=NOW,
        price=150.0,
        change_pct=1.69,
        volume=5000000,
        source="test",
        fetched_at=NOW,
    )
    df = quotes_to_df(quote)
    assert isinstance(df, pd.DataFrame)
    assert df.index.name == "symbol"
    assert df.loc["AAPL", "price"] == 150.0


def test_financials_to_df() -> None:
    stmt = IncomeStatement(
        symbol="AAPL",
        period="annual",
        fiscal_date=date(2023, 9, 30),
        revenue=383000000000.0,
        cost_of_revenue=214000000000.0,
        gross_profit=169000000000.0,
        operating_income=114000000000.0,
        net_income=97000000000.0,
        eps_basic=6.16,
        eps_diluted=6.13,
        currency="USD",
        source="test",
        fetched_at=NOW,
    )
    df = financials_to_df([stmt])
    assert isinstance(df, pd.DataFrame)
    assert df.index.name == "fiscal_date"
    assert df.loc[date(2023, 9, 30), "revenue"] == 383000000000.0


def test_indicators_to_df() -> None:
    indicators = TechnicalIndicators(
        ma20=150.0,
        ma60=140.0,
        rsi14=60.0,
    )
    df = indicators_to_df(indicators)
    assert isinstance(df, pd.DataFrame)
    assert df.loc[0, "rsi14"] == 60.0


def test_options_to_df() -> None:
    calls = [
        OptionContract(
            contract_symbol="AAPL240620C00150000",
            strike=150.0,
            last_price=5.0,
        )
    ]
    puts = [
        OptionContract(
            contract_symbol="AAPL240620P00150000",
            strike=150.0,
            last_price=4.0,
        )
    ]
    chain = OptionChain(
        symbol="AAPL",
        expiration_date=date(2024, 6, 20),
        calls=calls,
        puts=puts,
        source="test",
        fetched_at=NOW,
    )
    calls_df, puts_df = options_to_df(chain)
    assert isinstance(calls_df, pd.DataFrame)
    assert isinstance(puts_df, pd.DataFrame)
    assert calls_df.loc[150.0, "last_price"] == 5.0
    assert puts_df.loc[150.0, "last_price"] == 4.0


def test_generic_to_df() -> None:
    bars = [
        PriceBar(
            symbol="AAPL",
            date=date(2024, 1, 2),
            open=100.0,
            high=105.0,
            low=99.0,
            close=104.0,
            adj_close=104.0,
            volume=1000,
            source="test",
            fetched_at=NOW,
        )
    ]
    df = to_df(bars)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 1
