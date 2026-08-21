"""DataFrame conversion utilities for OneFinance models."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pandas as pd  # type: ignore[import-untyped]

from onefinance.core.models import (
    BalanceSheet,
    CashFlow,
    IncomeStatement,
    OptionChain,
    PriceBar,
    Quote,
)
from onefinance.indicators.core import TechnicalIndicators


def bars_to_df(bars: Sequence[PriceBar]) -> pd.DataFrame:
    """Convert a sequence of ``PriceBar`` models to a pandas DataFrame indexed by date."""
    if not bars:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume", "adj_close", "symbol", "source"]
        )
    records = [
        {
            "date": bar.date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "adj_close": bar.adj_close,
            "symbol": bar.symbol,
            "source": bar.source,
        }
        for bar in bars
    ]
    df = pd.DataFrame.from_records(records)
    df.set_index("date", inplace=True)
    df.sort_index(inplace=True)
    return df


def quotes_to_df(quotes: Sequence[Quote] | Quote) -> pd.DataFrame:
    """Convert one or more ``Quote`` models to a pandas DataFrame indexed by symbol."""
    seq = [quotes] if isinstance(quotes, Quote) else quotes
    if not seq:
        return pd.DataFrame(
            columns=[
                "price",
                "change_pct",
                "volume",
                "prev_close",
                "timestamp",
                "bid",
                "ask",
                "nav",
                "market_cap",
                "source",
            ]
        )
    records = [
        {
            "symbol": q.symbol,
            "price": q.price,
            "change_pct": q.change_pct,
            "volume": q.volume,
            "prev_close": q.prev_close,
            "timestamp": q.timestamp,
            "bid": q.bid,
            "ask": q.ask,
            "nav": q.nav,
            "market_cap": q.market_cap,
            "source": q.source,
        }
        for q in seq
    ]
    df = pd.DataFrame.from_records(records)
    df.set_index("symbol", inplace=True)
    return df


def financials_to_df(
    statements: Sequence[IncomeStatement | BalanceSheet | CashFlow],
) -> pd.DataFrame:
    """Convert financial statements to a pandas DataFrame indexed by period/date."""
    if not statements:
        return pd.DataFrame()
    records = []
    for stmt in statements:
        d = stmt.model_dump(mode="python")
        records.append(d)
    df = pd.DataFrame.from_records(records)
    if "fiscal_date" in df.columns:
        df.set_index("fiscal_date", inplace=True)
        df.sort_index(ascending=False, inplace=True)
    elif "period" in df.columns:
        df.set_index("period", inplace=True)
    return df


def indicators_to_df(indicators: TechnicalIndicators) -> pd.DataFrame:
    """Convert a ``TechnicalIndicators`` snapshot model to a single-row pandas DataFrame."""
    d = indicators.model_dump(mode="python")
    df = pd.DataFrame([d])
    if "symbol" in df.columns:
        df.set_index("symbol", inplace=True)
    return df


def options_to_df(chain: OptionChain) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Convert an ``OptionChain`` model to a pair of (calls_df, puts_df) DataFrames."""
    calls_records = [c.model_dump(mode="python") for c in chain.calls]
    puts_records = [p.model_dump(mode="python") for p in chain.puts]

    calls_df = pd.DataFrame.from_records(calls_records) if calls_records else pd.DataFrame()
    puts_df = pd.DataFrame.from_records(puts_records) if puts_records else pd.DataFrame()

    if not calls_df.empty and "strike" in calls_df.columns:
        calls_df.set_index("strike", inplace=True)
        calls_df.sort_index(inplace=True)
    if not puts_df.empty and "strike" in puts_df.columns:
        puts_df.set_index("strike", inplace=True)
        puts_df.sort_index(inplace=True)

    return calls_df, puts_df


def to_df(data: Any) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
    """Generic DataFrame converter dispatcher for OneFinance models."""
    if isinstance(data, OptionChain):
        return options_to_df(data)
    if isinstance(data, TechnicalIndicators):
        return indicators_to_df(data)
    if isinstance(data, Quote):
        return quotes_to_df(data)

    if isinstance(data, list):
        if not data:
            return pd.DataFrame()
        first = data[0]
        if isinstance(first, PriceBar):
            return bars_to_df(cast(list[PriceBar], data))
        if isinstance(first, Quote):
            return quotes_to_df(cast(list[Quote], data))
        if isinstance(first, (IncomeStatement, BalanceSheet, CashFlow)):
            return financials_to_df(cast(list[IncomeStatement | BalanceSheet | CashFlow], data))
        if hasattr(first, "model_dump"):
            records = [item.model_dump(mode="python") for item in data]
            return pd.DataFrame.from_records(records)

    if hasattr(data, "model_dump"):
        return pd.DataFrame([data.model_dump(mode="python")])

    raise ValueError(f"Cannot convert type {type(data)} to DataFrame")


__all__ = [
    "bars_to_df",
    "quotes_to_df",
    "financials_to_df",
    "indicators_to_df",
    "options_to_df",
    "to_df",
]
