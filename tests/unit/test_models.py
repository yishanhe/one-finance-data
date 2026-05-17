"""Unit tests for Pydantic data models."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from onefinance.core.models import (
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    EarningsRecord,
    FinancialRatios,
    IncomeStatement,
    InsiderTrade,
    PriceBar,
    Quote,
)

NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


# -----------------------------------------------------------------------
# PriceBar
# -----------------------------------------------------------------------


class TestPriceBar:
    def test_valid(self):
        bar = PriceBar(
            symbol="AAPL",
            date=date(2024, 1, 2),
            open=185.0,
            high=186.5,
            low=184.0,
            close=185.64,
            adj_close=185.64,
            volume=50_000_000,
            source="yfinance",
            fetched_at=NOW,
        )
        assert bar.symbol == "AAPL"
        assert bar.close == 185.64
        assert bar.volume == 50_000_000

    def test_frozen(self):
        bar = PriceBar(
            symbol="AAPL",
            date=date(2024, 1, 2),
            open=185.0,
            high=186.5,
            low=184.0,
            close=185.64,
            adj_close=185.64,
            volume=50_000_000,
            source="yfinance",
            fetched_at=NOW,
        )
        with pytest.raises(ValidationError):
            bar.close = 999.0  # type: ignore[misc]

    def test_negative_price_rejected(self):
        with pytest.raises(ValidationError):
            PriceBar(
                symbol="AAPL",
                date=date(2024, 1, 2),
                open=-1.0,
                high=186.5,
                low=184.0,
                close=185.64,
                adj_close=185.64,
                volume=50_000_000,
                source="yfinance",
                fetched_at=NOW,
            )

    def test_negative_volume_rejected(self):
        with pytest.raises(ValidationError):
            PriceBar(
                symbol="AAPL",
                date=date(2024, 1, 2),
                open=185.0,
                high=186.5,
                low=184.0,
                close=185.64,
                adj_close=185.64,
                volume=-100,
                source="yfinance",
                fetched_at=NOW,
            )

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            PriceBar(
                symbol="AAPL",
                date=date(2024, 1, 2),
                open=185.0,
                high=186.5,
                low=184.0,
                close=185.64,
                adj_close=185.64,
                volume=50_000_000,
                source="yfinance",
                fetched_at=NOW,
                unknown_field="boom",  # type: ignore[call-arg]
            )

    def test_invalid_symbol_rejected(self):
        with pytest.raises(ValidationError):
            PriceBar(
                symbol="aapl",  # lowercase not allowed
                date=date(2024, 1, 2),
                open=185.0,
                high=186.5,
                low=184.0,
                close=185.64,
                adj_close=185.64,
                volume=50_000_000,
                source="yfinance",
                fetched_at=NOW,
            )

    def test_json_round_trip(self):
        bar = PriceBar(
            symbol="AAPL",
            date=date(2024, 1, 2),
            open=185.0,
            high=186.5,
            low=184.0,
            close=185.64,
            adj_close=185.64,
            volume=50_000_000,
            source="yfinance",
            fetched_at=NOW,
        )
        json_str = bar.model_dump_json()
        bar2 = PriceBar.model_validate_json(json_str)
        assert bar == bar2


# -----------------------------------------------------------------------
# Quote
# -----------------------------------------------------------------------


class TestQuote:
    def test_valid_with_optional_fields(self):
        q = Quote(
            symbol="MSFT",
            timestamp=NOW,
            price=420.50,
            bid=420.40,
            ask=420.60,
            volume=1_000_000,
            source="fmp",
            fetched_at=NOW,
        )
        assert q.bid == 420.40

    def test_valid_without_optional_fields(self):
        q = Quote(
            symbol="MSFT",
            timestamp=NOW,
            price=420.50,
            volume=1_000_000,
            source="fmp",
            fetched_at=NOW,
        )
        assert q.bid is None
        assert q.ask is None


# -----------------------------------------------------------------------
# IncomeStatement
# -----------------------------------------------------------------------


class TestIncomeStatement:
    def test_valid(self):
        stmt = IncomeStatement(
            symbol="AAPL",
            period="2024-FY",
            fiscal_date=date(2024, 9, 28),
            revenue=391_035_000_000.0,
            cost_of_revenue=214_137_000_000.0,
            gross_profit=176_898_000_000.0,
            operating_income=123_216_000_000.0,
            net_income=96_995_000_000.0,
            eps_basic=6.42,
            eps_diluted=6.33,
            currency="USD",
            source="fmp",
            fetched_at=NOW,
        )
        assert stmt.revenue > 0
        assert stmt.currency == "USD"

    def test_negative_revenue_allowed(self):
        """Companies can legitimately report negative revenue (design doc §8)."""
        stmt = IncomeStatement(
            symbol="AAPL",
            period="2024-Q1",
            fiscal_date=date(2024, 3, 31),
            revenue=-100_000.0,
            cost_of_revenue=50_000.0,
            gross_profit=-150_000.0,
            operating_income=-200_000.0,
            net_income=-300_000.0,
            eps_basic=-0.50,
            eps_diluted=-0.50,
            currency="USD",
            source="fmp",
            fetched_at=NOW,
        )
        assert stmt.revenue == -100_000.0

    def test_bad_currency_rejected(self):
        with pytest.raises(ValidationError):
            IncomeStatement(
                symbol="AAPL",
                period="2024-FY",
                fiscal_date=date(2024, 9, 28),
                revenue=1.0,
                cost_of_revenue=1.0,
                gross_profit=1.0,
                operating_income=1.0,
                net_income=1.0,
                eps_basic=1.0,
                eps_diluted=1.0,
                currency="US",  # too short
                source="fmp",
                fetched_at=NOW,
            )


# -----------------------------------------------------------------------
# CompanyInfo
# -----------------------------------------------------------------------


class TestCompanyInfo:
    def test_minimal(self):
        info = CompanyInfo(
            symbol="AAPL",
            name="Apple Inc.",
            source="yfinance",
            fetched_at=NOW,
        )
        assert info.sector is None
        assert info.market_cap is None

    def test_full(self):
        info = CompanyInfo(
            symbol="AAPL",
            name="Apple Inc.",
            exchange="NMS",
            sector="Technology",
            industry="Consumer Electronics",
            country="US",
            market_cap=3_000_000_000_000.0,
            description="Apple designs things.",
            website="https://apple.com",
            employees=164_000,
            currency="USD",
            source="yfinance",
            fetched_at=NOW,
        )
        assert info.employees == 164_000


# -----------------------------------------------------------------------
# BalanceSheet, CashFlow
# -----------------------------------------------------------------------


class TestBalanceSheet:
    def test_valid(self):
        bs = BalanceSheet(
            symbol="AAPL",
            period="2024-FY",
            fiscal_date=date(2024, 9, 28),
            total_assets=352_583_000_000.0,
            total_liabilities=290_437_000_000.0,
            total_equity=62_146_000_000.0,
            cash_and_equivalents=29_965_000_000.0,
            total_debt=104_590_000_000.0,
            currency="USD",
            source="fmp",
            fetched_at=NOW,
        )
        assert bs.total_assets > 0


class TestCashFlow:
    def test_valid(self):
        cf = CashFlow(
            symbol="AAPL",
            period="2024-FY",
            fiscal_date=date(2024, 9, 28),
            operating_cash_flow=110_543_000_000.0,
            capital_expenditure=-10_959_000_000.0,
            free_cash_flow=99_584_000_000.0,
            dividends_paid=-15_025_000_000.0,
            currency="USD",
            source="fmp",
            fetched_at=NOW,
        )
        assert cf.free_cash_flow > 0


# -----------------------------------------------------------------------
# FinancialRatios, EarningsRecord, InsiderTrade
# -----------------------------------------------------------------------


class TestFinancialRatios:
    def test_all_optional(self):
        r = FinancialRatios(
            symbol="AAPL",
            period="2024-FY",
            fiscal_date=date(2024, 9, 28),
            source="fmp",
            fetched_at=NOW,
        )
        assert r.pe_ratio is None


class TestEarningsRecord:
    def test_valid(self):
        e = EarningsRecord(
            symbol="AAPL",
            period="2024-Q4",
            fiscal_date=date(2024, 9, 28),
            eps_actual=1.64,
            eps_estimate=1.60,
            eps_surprise=0.04,
            source="fmp",
            fetched_at=NOW,
        )
        assert e.eps_surprise == 0.04


class TestInsiderTrade:
    def test_valid(self):
        t = InsiderTrade(
            symbol="AAPL",
            filing_date=date(2024, 8, 15),
            trade_date=date(2024, 8, 14),
            insider_name="Tim Cook",
            insider_title="CEO",
            trade_type="sell",
            shares=50_000.0,
            price_per_share=225.0,
            total_value=11_250_000.0,
            shares_owned_after=100_000.0,
            source="fmp",
            fetched_at=NOW,
        )
        assert t.trade_type == "sell"

    def test_negative_shares_rejected(self):
        with pytest.raises(ValidationError):
            InsiderTrade(
                symbol="AAPL",
                filing_date=date(2024, 8, 15),
                insider_name="Tim Cook",
                trade_type="sell",
                shares=-100.0,
                source="fmp",
                fetched_at=NOW,
            )
