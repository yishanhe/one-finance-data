"""Pydantic data models for all financial data types.

All models are frozen (immutable) and forbid extra fields so that
provider schema drift fails loudly at validation time rather than
silently dropping data.

Every model carries ``source`` (which provider answered) and
``fetched_at`` (UTC timestamp when the provider adapter normalised the
response) for full audit trail.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Base model & common annotated types
# ---------------------------------------------------------------------------


class FinanceModel(BaseModel):
    """Shared config for all finance data models."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


Symbol = Annotated[str, Field(min_length=1, max_length=20, pattern=r"^[A-Z0-9.\-]+$")]
Currency = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]


# ---------------------------------------------------------------------------
# Price / Quote
# ---------------------------------------------------------------------------


class PriceBar(FinanceModel):
    """Single OHLCV bar for a trading day (or intraday interval)."""

    symbol: Symbol
    date: date
    timestamp: datetime | None = Field(default=None, description="Exact time for intraday bars")
    open: Annotated[float, Field(ge=0)]
    high: Annotated[float, Field(ge=0)]
    low: Annotated[float, Field(ge=0)]
    close: Annotated[float, Field(ge=0)]
    adj_close: Annotated[float, Field(ge=0)]
    volume: Annotated[int, Field(ge=0)]
    source: str
    fetched_at: datetime


class Quote(FinanceModel):
    """Real-time (or near-realtime) quote snapshot."""

    symbol: Symbol
    timestamp: datetime
    price: Annotated[float, Field(ge=0)]
    bid: Annotated[float, Field(ge=0)] | None = None
    ask: Annotated[float, Field(ge=0)] | None = None
    volume: Annotated[int, Field(ge=0)]
    nav: float | None = None
    market_cap: float | None = None
    source: str
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Financial statements
# ---------------------------------------------------------------------------


class IncomeStatement(FinanceModel):
    """Normalised income statement for a fiscal period."""

    symbol: Symbol
    period: str  # "2024-Q3" or "2024-FY"
    fiscal_date: date
    revenue: float
    cost_of_revenue: float
    gross_profit: float
    operating_income: float
    net_income: float
    eps_basic: float
    eps_diluted: float
    currency: Currency
    source: str
    fetched_at: datetime
    # Extended fields (optional — not all providers supply these)
    ebitda: float | None = None
    research_and_development: float | None = None
    sga_expenses: float | None = None


class BalanceSheet(FinanceModel):
    """Normalised balance sheet for a fiscal period."""

    symbol: Symbol
    period: str
    fiscal_date: date
    total_assets: float
    total_liabilities: float
    total_equity: float
    cash_and_equivalents: float
    total_debt: float
    currency: Currency
    source: str
    fetched_at: datetime
    # Extended fields
    short_term_investments: float | None = None
    total_current_assets: float | None = None
    total_current_liabilities: float | None = None
    net_debt: float | None = None
    goodwill: float | None = None
    inventory: float | None = None


class CashFlow(FinanceModel):
    """Normalised cash-flow statement for a fiscal period."""

    symbol: Symbol
    period: str
    fiscal_date: date
    operating_cash_flow: float
    capital_expenditure: float
    free_cash_flow: float
    dividends_paid: float
    currency: Currency
    source: str
    fetched_at: datetime
    # Extended fields
    depreciation_and_amortization: float | None = None
    change_in_working_capital: float | None = None
    stock_based_compensation: float | None = None
    common_stock_repurchased: float | None = None


# ---------------------------------------------------------------------------
# Company info
# ---------------------------------------------------------------------------


class CompanyInfo(FinanceModel):
    """Static / slow-changing company profile information."""

    symbol: Symbol
    name: str
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    market_cap: float | None = None
    beta: float | None = None
    shares_outstanding: int | None = None
    description: str | None = None
    website: str | None = None
    employees: int | None = None
    currency: Currency | None = None
    source: str
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Ratios, earnings, insider trades
# ---------------------------------------------------------------------------


class FinancialRatios(FinanceModel):
    """Key financial ratios for a fiscal period."""

    symbol: Symbol
    period: str
    fiscal_date: date
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    ps_ratio: float | None = None
    debt_to_equity: float | None = None
    current_ratio: float | None = None
    return_on_equity: float | None = None
    return_on_assets: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    dividend_yield: float | None = None
    # Extended fields (key metrics)
    enterprise_value: float | None = None
    roic: float | None = None
    ev_to_ebitda: float | None = None
    ev_to_sales: float | None = None
    free_cash_flow_yield: float | None = None
    book_value_per_share: float | None = None
    revenue_per_share: float | None = None
    quick_ratio: float | None = None
    interest_coverage: float | None = None
    source: str
    fetched_at: datetime


class EarningsRecord(FinanceModel):
    """Earnings report — actual vs estimate for a fiscal period."""

    symbol: Symbol
    period: str
    fiscal_date: date
    eps_actual: float | None = None
    eps_estimate: float | None = None
    eps_surprise: float | None = None
    revenue_actual: float | None = None
    revenue_estimate: float | None = None
    source: str
    fetched_at: datetime


class ForwardEstimates(FinanceModel):
    """Forward-looking analyst estimates for future periods."""

    symbol: Symbol
    period: str  # e.g., "+1y", "2025-FY"
    fiscal_date: date | None = None
    eps_estimate: float | None = None
    revenue_estimate: float | None = None
    revenue_growth: float | None = None  # e.g., 0.15 for 15%
    source: str
    fetched_at: datetime


class InsiderTrade(FinanceModel):
    """SEC Form 4 insider trade filing."""

    symbol: Symbol
    filing_date: date
    trade_date: date | None = None
    insider_name: str
    insider_title: str | None = None
    trade_type: str  # "buy", "sell", "exercise"
    shares: Annotated[float, Field(ge=0)]
    price_per_share: Annotated[float, Field(ge=0)] | None = None
    total_value: float | None = None
    shares_owned_after: float | None = None
    source: str
    fetched_at: datetime


# ---------------------------------------------------------------------------
# DCF Valuation
# ---------------------------------------------------------------------------


class DCFValuation(FinanceModel):
    """Discounted cash flow valuation estimate."""

    symbol: Symbol
    dcf: float
    stock_price: float
    dcf_date: date
    source: str
    fetched_at: datetime

    @property
    def upside_pct(self) -> float | None:
        """Percentage upside from current price to DCF value."""
        if self.stock_price > 0:
            return round((self.dcf / self.stock_price - 1) * 100, 1)
        return None


# ---------------------------------------------------------------------------
# Alternative & Institutional Data
# ---------------------------------------------------------------------------


class NewsArticle(FinanceModel):
    """A financial news article or press release."""

    symbol: Symbol
    title: str
    publisher: str
    link: str
    published_at: datetime
    summary: str | None = None
    source: str
    fetched_at: datetime


class CorporateAction(FinanceModel):
    """Dividend payment or stock split event."""

    symbol: Symbol
    date: date
    action_type: str  # e.g., "dividend" or "split"
    amount: float | None = None  # Dividend amount
    split_ratio: float | None = None  # e.g., 4.0 for a 4-for-1 split
    source: str
    fetched_at: datetime


class InstitutionalHolder(FinanceModel):
    """Institutional ownership record (e.g., from 13F filings)."""

    symbol: Symbol
    holder_name: str
    shares: int
    value: float | None = None
    change: int | None = None
    change_pct: float | None = None
    source: str
    fetched_at: datetime


class AnalystData(FinanceModel):
    """Analyst ratings and price targets."""

    symbol: Symbol
    target_high: float | None = None
    target_low: float | None = None
    target_mean: float | None = None
    target_median: float | None = None
    rating_buy: int | None = None
    rating_hold: int | None = None
    rating_sell: int | None = None
    rating_strong_buy: int | None = None
    rating_strong_sell: int | None = None
    source: str
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


class OptionContract(FinanceModel):
    """A single options contract (call or put).

    Greek fields (``delta`` … ``rho``) and ``smv_vol`` are optional and only
    populated by providers that source them (e.g. Tradier via ORATS). Providers
    that return chains without greeks (yfinance, Massive) leave them ``None``.
    ``smv_vol`` is ORATS's smoothed market volatility — preferred over raw
    ``implied_volatility`` as the vol input for variance/VIX-style calculations.
    """

    contract_symbol: str
    strike: float
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: float | None = None
    in_the_money: bool | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    smv_vol: float | None = None


class OptionChain(FinanceModel):
    """The complete option chain for a specific expiration date."""

    symbol: Symbol
    expiration_date: date
    calls: list[OptionContract]
    puts: list[OptionContract]
    source: str
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Earnings Calendar
# ---------------------------------------------------------------------------


class EarningsCalendarEntry(FinanceModel):
    """Scheduled or recent earnings report for a single company."""

    symbol: Symbol
    report_date: date
    year: int | None = None
    quarter: int | None = None
    eps_estimate: float | None = None
    eps_actual: float | None = None
    revenue_estimate: float | None = None
    revenue_actual: float | None = None
    time_of_day: str | None = None  # "bmo", "amc", "dmh", or None
    source: str
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Economic (Macro) Calendar
# ---------------------------------------------------------------------------


class EconomicEvent(FinanceModel):
    """A scheduled macroeconomic data release or central bank event."""

    event: str
    event_date: date
    event_time: str | None = None  # HH:MM UTC when known
    country: str | None = None  # ISO 3166-1 alpha-2 (e.g. "US", "EU")
    currency: str | None = None  # currency affected (e.g. "USD")
    unit: str | None = None  # e.g. "%", "K", "B"
    estimate: float | None = None
    actual: float | None = None
    previous: float | None = None
    impact: str | None = None  # "high", "medium", "low", or None
    source: str
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Peer Companies
# ---------------------------------------------------------------------------


class PeerCompany(FinanceModel):
    """A company identified as a peer or competitor."""

    symbol: Symbol
    name: str | None = None
    price: float | None = None
    market_cap: int | None = None
    source: str
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Market Screeners & Sector Overviews
# ---------------------------------------------------------------------------


class ScreenerResult(FinanceModel):
    """A single equity returned by a stock screener query."""

    symbol: Symbol
    company_name: str | None = None
    market_cap: float | None = None
    sector: str | None = None
    industry: str | None = None
    price: float | None = None
    volume: int | None = None
    source: str
    fetched_at: datetime


class SectorInfo(FinanceModel):
    """Overview of a market sector or industry."""

    name: str
    market_weight: float | None = None
    ytd_return: float | None = None
    top_companies: list[str] | None = None
    source: str
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Sentiment & Short Interest
# ---------------------------------------------------------------------------


class OptionsAnalytics(FinanceModel):
    """Aggregated put/call metrics across option expirations for a symbol."""

    symbol: str
    pcr_volume: float | None = None
    pcr_oi: float | None = None
    total_put_volume: int = 0
    total_call_volume: int = 0
    total_put_oi: int = 0
    total_call_oi: int = 0
    expirations_used: int = 0
    source: str
    fetched_at: datetime


class ShortInterest(FinanceModel):
    """Short interest data for a symbol."""

    symbol: str
    short_interest: int | None = None
    short_float_pct: float | None = None
    days_to_cover: float | None = None
    settlement_date: date | None = None
    source: str
    fetched_at: datetime


class MarketSentiment(FinanceModel):
    """Market-wide put/call ratio data."""

    pcr_equity: float | None = None
    pcr_index: float | None = None
    pcr_total: float | None = None
    as_of_date: date | None = None
    source: str
    fetched_at: datetime
