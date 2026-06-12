"""YFinance provider adapter.

Wraps the ``yfinance`` library to provide ``get_price_history`` and
``get_info`` endpoints.  yfinance is an unofficial Yahoo Finance
scraper — it's free and unlimited but can break without notice,
so it's always the last-resort tier.

M1 scope: price_history + info only.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd  # type: ignore[import-untyped]
import yfinance as yf  # type: ignore[import-untyped]

from onefinance.core.errors import ProviderError
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    EarningsRecord,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    NewsArticle,
    OptionChain,
    OptionContract,
    PriceBar,
    Quote,
    SectorInfo,
    ShortInterest,
)
from onefinance.providers._utils import (
    _safe_float,
    _safe_int,
    format_period,
    normalize_symbol,
    quarter_from_date,
    utc_now,
)
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "yfinance"


def _df_get(df: Any, key: str, col: Any) -> float:
    """Return float from yfinance financial DataFrame cell, 0.0 if missing/NaN."""
    try:
        val = df.at[key, col]
        return float(val) if pd.notna(val) else 0.0
    except (KeyError, TypeError, ValueError):
        return 0.0


def _df_get_opt(df: Any, key: str, col: Any) -> float | None:
    """Return optional float from yfinance financial DataFrame cell."""
    try:
        val = df.at[key, col]
        return float(val) if pd.notna(val) else None
    except (KeyError, TypeError, ValueError):
        return None


def _yf_period_label(fiscal_date: date, quarterly: bool) -> str:
    year = fiscal_date.year
    if not quarterly:
        return f"{year}-FY"
    q = (fiscal_date.month - 1) // 3 + 1
    return f"{year}-Q{q}"


class YFinanceProvider(BaseProvider):
    """Provider adapter for yfinance (unofficial Yahoo Finance scraper)."""

    name = _SOURCE

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout

    # -------------------------------------------------------------------
    # get_price_history — Type A (historical)
    # -------------------------------------------------------------------

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[PriceBar]:
        """Fetch OHLCV bars via yfinance.

        Parameters
        ----------
        symbol : str
            Ticker symbol (e.g. ``"AAPL"``).
        start, end : date
            Inclusive date range.
        interval : str
            Bar interval — ``"1d"``, ``"1wk"``, ``"1mo"`` etc.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            df = ticker.history(
                start=start.isoformat(),
                end=end.isoformat(),
                interval=interval,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if df is None or df.empty:
            return []

        bars: list[PriceBar] = []
        for idx, row in df.iterrows():
            try:
                # idx is a pandas Timestamp. If tz-aware, it has intraday time.
                bar_date = idx.date() if hasattr(idx, "date") else idx
                bar_ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else None
                bars.append(
                    PriceBar(
                        symbol=sym,
                        date=bar_date,
                        timestamp=bar_ts,
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        # yfinance may not have Adj Close in newer versions;
                        # fall back to Close.
                        adj_close=float(row.get("Adj Close", row.get("Close", row["Close"]))),
                        volume=int(row["Volume"]),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Skipping bar for %s on %s: %s",
                    symbol,
                    idx,
                    exc,
                )
                continue

        return bars

    # -------------------------------------------------------------------
    # get_quote — Type B (live-ish)
    # -------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        """Fetch current quote snapshot via yf.Ticker.info."""
        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            info = ticker.info or {}
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance quote failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if not info or info.get("quoteType") is None:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No quote found for symbol '{symbol}' via yfinance",
                provider=self.name,
                retry_safe=False,
            )

        return Quote(
            symbol=sym,
            timestamp=now,
            price=float(info.get("currentPrice") or info.get("regularMarketPrice") or 0.0),
            bid=_safe_float(info.get("bid")),
            ask=_safe_float(info.get("ask")),
            volume=_safe_int(info.get("volume") or info.get("regularMarketVolume") or 0),
            nav=_safe_float(info.get("navPrice")),
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # get_info — Type A (slow-changing)
    # -------------------------------------------------------------------

    def get_info(self, symbol: str) -> CompanyInfo:
        """Fetch company profile via yfinance's ``.info`` dict."""
        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            info: dict[str, Any] = ticker.info or {}
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance .info failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if not info or info.get("quoteType") is None:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No info found for symbol '{symbol}' via yfinance",
                provider=self.name,
                retry_safe=False,
            )

        # Normalise currency — yfinance returns e.g. "USD", sometimes None
        raw_currency = info.get("currency")
        currency: str | None = None
        if raw_currency and isinstance(raw_currency, str) and len(raw_currency) == 3:
            currency = raw_currency.upper()

        return CompanyInfo(
            symbol=sym,
            name=info.get("longName") or info.get("shortName") or symbol,
            exchange=info.get("exchange"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            country=info.get("country"),
            market_cap=_safe_float(info.get("marketCap")),
            beta=_safe_float(info.get("beta")),
            shares_outstanding=_safe_int(info.get("sharesOutstanding")),
            description=info.get("longBusinessSummary"),
            website=info.get("website"),
            employees=_safe_int(info.get("fullTimeEmployees")),
            currency=currency,
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # Alternative Data Endpoints
    # -------------------------------------------------------------------

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        """Fetch news from yfinance."""
        from onefinance.core.models import NewsArticle

        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            raw_news = ticker.news or []
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance news failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        articles = []
        for n in raw_news[:limit]:
            try:
                published_at = datetime.fromtimestamp(n.get("providerPublishTime", 0), UTC)
                articles.append(
                    NewsArticle(
                        symbol=sym,
                        title=n.get("title", ""),
                        publisher=n.get("publisher", ""),
                        link=n.get("link", ""),
                        published_at=published_at,
                        summary=n.get("summary") or n.get("relatedTickers"),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Failed to parse news for %s: %s", symbol, exc)
                continue
        return articles

    def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Fetch dividends and splits from yfinance."""
        from onefinance.core.models import CorporateAction

        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            divs = ticker.dividends
            splits = ticker.splits
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance corporate actions failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        actions = []
        if divs is not None and not divs.empty:
            for dt, val in divs.items():
                date_val = dt.date() if hasattr(dt, "date") else dt
                actions.append(
                    CorporateAction(
                        symbol=sym,
                        date=date_val,
                        action_type="dividend",
                        amount=float(val),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )

        if splits is not None and not splits.empty:
            for dt, val in splits.items():
                date_val = dt.date() if hasattr(dt, "date") else dt
                actions.append(
                    CorporateAction(
                        symbol=sym,
                        date=date_val,
                        action_type="split",
                        split_ratio=float(val),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )

        # Sort by date descending
        return sorted(actions, key=lambda a: a.date, reverse=True)

    def get_institutional_holders(self, symbol: str) -> list[InstitutionalHolder]:
        """Fetch institutional holders from yfinance."""
        from onefinance.core.models import InstitutionalHolder

        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            df = ticker.institutional_holders
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance institutional holders failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if df is None or df.empty:
            return []

        holders = []
        for _, row in df.iterrows():
            try:
                holders.append(
                    InstitutionalHolder(
                        symbol=sym,
                        holder_name=str(row.get("Holder", "")),
                        shares=int(row.get("Shares", 0)),
                        value=float(row.get("Value", 0)),
                        change=int(row.get("Date Reported", 0))
                        if "Date Reported" in row
                        else None,  # yfinance often lacks exact changes here
                        change_pct=float(row.get("% Out", 0)) if "% Out" in row else None,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Failed to parse holder for %s: %s", symbol, exc)
                continue

        return holders

    def get_analyst_data(self, symbol: str) -> AnalystData:
        """Fetch analyst ratings from yfinance info."""
        from onefinance.core.models import AnalystData

        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            info = ticker.info or {}
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance analyst info failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        return AnalystData(
            symbol=sym,
            target_high=_safe_float(info.get("targetHighPrice")),
            target_low=_safe_float(info.get("targetLowPrice")),
            target_mean=_safe_float(info.get("targetMeanPrice")),
            target_median=_safe_float(info.get("targetMedianPrice")),
            rating_buy=_safe_int(
                info.get("numberOfAnalystOpinions")
            ),  # yfinance doesn't break out strong buy etc reliably in .info
            source=_SOURCE,
            fetched_at=now,
        )

    def get_options_expirations(self, symbol: str) -> list[date]:
        """Fetch available option expiration dates from yfinance."""
        ticker = yf.Ticker(symbol)
        try:
            dates = ticker.options
            return [date.fromisoformat(d) for d in dates]
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance options failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

    def get_option_chain(self, symbol: str, expiration: date) -> OptionChain:
        """Fetch the option chain for a specific expiration date."""
        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)
        date_str = expiration.isoformat()

        try:
            chain = ticker.option_chain(date_str)
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance option chain failed for {symbol} on {date_str}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        def _parse_contract(row: Any) -> OptionContract:
            return OptionContract(
                contract_symbol=str(row.get("contractSymbol", "")),
                strike=float(row.get("strike", 0.0)),
                last_price=_safe_float(row.get("lastPrice")),
                bid=_safe_float(row.get("bid")),
                ask=_safe_float(row.get("ask")),
                volume=_safe_int(row.get("volume")),
                open_interest=_safe_int(row.get("openInterest")),
                implied_volatility=_safe_float(row.get("impliedVolatility")),
                in_the_money=bool(row.get("inTheMoney")) if "inTheMoney" in row else None,
            )

        calls = (
            [_parse_contract(row) for _, row in chain.calls.iterrows()]
            if not chain.calls.empty
            else []
        )
        puts = (
            [_parse_contract(row) for _, row in chain.puts.iterrows()]
            if not chain.puts.empty
            else []
        )

        return OptionChain(
            symbol=sym,
            expiration_date=expiration,
            calls=calls,
            puts=puts,
            source=_SOURCE,
            fetched_at=now,
        )

    def get_sector_overview(self, sector: str) -> SectorInfo:
        """Fetch sector overview using yf.Sector."""
        now = utc_now()

        try:
            sec = yf.Sector(sector.lower())
            overview = sec.overview or {}
            top_df = sec.top_companies
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance sector failed for {sector}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        top_companies = []
        if top_df is not None and not top_df.empty:
            top_companies = top_df.index.tolist()[:20]

        return SectorInfo(
            name=sector.title(),
            market_weight=_safe_float(overview.get("market_weight")),
            ytd_return=None,  # Not provided in overview directly
            top_companies=top_companies,
            source=_SOURCE,
            fetched_at=now,
        )

    def get_forward_estimates(self, symbol: str) -> list[ForwardEstimates]:
        """Fetch analyst estimates from yfinance."""
        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            # yfinance returns DataFrames for these
            rev_est = ticker.revenue_estimate
            eps_est = ticker.earnings_estimate
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance estimates failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        results = []
        # revenue_estimate typically has index like ['0q', '+1q', '0y', '+1y']
        # Columns like ['avg', 'low', 'high', 'year_ago_rev', 'growth']
        if rev_est is not None and not rev_est.empty:
            for period_label, row in rev_est.iterrows():
                # We also want EPS if available for same period
                eps_val = None
                if eps_est is not None and period_label in eps_est.index:
                    eps_val = _safe_float(eps_est.loc[period_label, "avg"])

                results.append(
                    ForwardEstimates(
                        symbol=sym,
                        period=str(period_label),
                        fiscal_date=None,  # yfinance estimate frames don't always have exact dates
                        eps_estimate=eps_val,
                        revenue_estimate=_safe_float(row.get("avg")),
                        revenue_growth=_safe_float(row.get("growth")),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )

        return results

    # -------------------------------------------------------------------
    # get_financials — Type A
    # -------------------------------------------------------------------

    def get_financials(
        self,
        symbol: str,
        statement: str,
        period: str,
    ) -> list[IncomeStatement | BalanceSheet | CashFlow]:
        """Fetch financial statements from yfinance DataFrames.

        Parameters
        ----------
        statement : str
            One of ``"income"``, ``"balance"``, ``"cashflow"``.
        period : str
            ``"annual"`` or ``"quarterly"``.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)
        is_quarterly = period == "quarterly"

        try:
            if statement == "income":
                df = ticker.quarterly_income_stmt if is_quarterly else ticker.income_stmt
            elif statement == "balance":
                df = ticker.quarterly_balance_sheet if is_quarterly else ticker.balance_sheet
            elif statement == "cashflow":
                df = ticker.quarterly_cashflow if is_quarterly else ticker.cashflow
            else:
                raise ProviderError(
                    code="INVALID_ARGUMENT",
                    message=(
                        f"Unknown statement type: '{statement}'. "
                        "Use 'income', 'balance', or 'cashflow'."
                    ),
                    provider=self.name,
                    retry_safe=False,
                )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance financials failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if df is None or df.empty:
            return []

        # Currency: yfinance DataFrames don't carry currency; default to USD.
        try:
            raw = (ticker.info or {}).get("currency") or "USD"
            currency = raw.upper()[:3]
        except Exception:
            currency = "USD"

        results: list[IncomeStatement | BalanceSheet | CashFlow] = []
        for col in df.columns:
            col_date: date = col.date() if hasattr(col, "date") else col
            period_str = _yf_period_label(col_date, is_quarterly)
            try:
                if statement == "income":
                    results.append(
                        IncomeStatement(
                            symbol=sym,
                            period=period_str,
                            fiscal_date=col_date,
                            revenue=_df_get(df, "Total Revenue", col),
                            cost_of_revenue=_df_get(df, "Cost Of Revenue", col),
                            gross_profit=_df_get(df, "Gross Profit", col),
                            operating_income=_df_get(df, "Operating Income", col),
                            net_income=_df_get(df, "Net Income", col),
                            eps_basic=_df_get(df, "Basic EPS", col),
                            eps_diluted=_df_get(df, "Diluted EPS", col),
                            currency=currency,
                            source=_SOURCE,
                            fetched_at=now,
                            ebitda=_df_get_opt(df, "EBITDA", col),
                            research_and_development=_df_get_opt(
                                df, "Research And Development", col
                            ),
                            sga_expenses=_df_get_opt(df, "Selling General Administrative", col),
                        )
                    )
                elif statement == "balance":
                    results.append(
                        BalanceSheet(
                            symbol=sym,
                            period=period_str,
                            fiscal_date=col_date,
                            total_assets=_df_get(df, "Total Assets", col),
                            total_liabilities=_df_get(
                                df, "Total Liabilities Net Minority Interest", col
                            ),
                            total_equity=_df_get(df, "Total Equity Gross Minority Interest", col),
                            cash_and_equivalents=_df_get(df, "Cash And Cash Equivalents", col),
                            total_debt=_df_get(df, "Total Debt", col),
                            currency=currency,
                            source=_SOURCE,
                            fetched_at=now,
                            short_term_investments=_df_get_opt(df, "Short Term Investments", col),
                            total_current_assets=_df_get_opt(df, "Current Assets", col),
                            total_current_liabilities=_df_get_opt(df, "Current Liabilities", col),
                            net_debt=_df_get_opt(df, "Net Debt", col),
                            goodwill=_df_get_opt(df, "Goodwill", col),
                            inventory=_df_get_opt(df, "Inventory", col),
                        )
                    )
                else:
                    results.append(
                        CashFlow(
                            symbol=sym,
                            period=period_str,
                            fiscal_date=col_date,
                            operating_cash_flow=_df_get(df, "Operating Cash Flow", col),
                            capital_expenditure=_df_get(df, "Capital Expenditure", col),
                            free_cash_flow=_df_get(df, "Free Cash Flow", col),
                            dividends_paid=_df_get(df, "Common Stock Dividend Paid", col),
                            currency=currency,
                            source=_SOURCE,
                            fetched_at=now,
                            depreciation_and_amortization=_df_get_opt(
                                df, "Depreciation And Amortization", col
                            ),
                        )
                    )
            except Exception as exc:
                logger.warning("Skipping %s row for %s at %s: %s", statement, symbol, col_date, exc)
                continue

        return results

    # -------------------------------------------------------------------
    # get_ratios — Type A (TTM snapshot from info)
    # -------------------------------------------------------------------

    def get_ratios(self, symbol: str, period: str) -> list[FinancialRatios]:
        """Fetch current TTM ratios from yfinance .info.

        yfinance only provides a live snapshot; *period* is accepted for
        interface compatibility but always yields a single ``"TTM"`` record.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            info = ticker.info or {}
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance ratios failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if not info or info.get("quoteType") is None:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No data found for '{symbol}' via yfinance",
                provider=self.name,
                retry_safe=False,
            )

        return [
            FinancialRatios(
                symbol=sym,
                period="TTM",
                fiscal_date=now.date(),
                pe_ratio=_safe_float(info.get("trailingPE")),
                pb_ratio=_safe_float(info.get("priceToBook")),
                ps_ratio=_safe_float(info.get("priceToSalesTrailing12Months")),
                debt_to_equity=_safe_float(info.get("debtToEquity")),
                current_ratio=_safe_float(info.get("currentRatio")),
                return_on_equity=_safe_float(info.get("returnOnEquity")),
                return_on_assets=_safe_float(info.get("returnOnAssets")),
                gross_margin=_safe_float(info.get("grossMargins")),
                operating_margin=_safe_float(info.get("operatingMargins")),
                net_margin=_safe_float(info.get("profitMargins")),
                dividend_yield=_safe_float(info.get("dividendYield")),
                enterprise_value=_safe_float(info.get("enterpriseValue")),
                ev_to_ebitda=_safe_float(info.get("enterpriseToEbitda")),
                ev_to_sales=_safe_float(info.get("enterpriseToRevenue")),
                book_value_per_share=_safe_float(info.get("bookValue")),
                revenue_per_share=_safe_float(info.get("revenuePerShare")),
                quick_ratio=_safe_float(info.get("quickRatio")),
                source=_SOURCE,
                fetched_at=now,
            )
        ]

    # -------------------------------------------------------------------
    # get_earnings — Type C
    # -------------------------------------------------------------------

    def get_earnings(self, symbol: str) -> list[EarningsRecord]:
        """Fetch quarterly earnings history from yfinance earnings_history DataFrame.

        Revenue fields are not available via this source and will be ``None``.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            df = ticker.earnings_history
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance earnings failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if df is None or df.empty:
            return []

        results: list[EarningsRecord] = []
        for idx, row in df.iterrows():
            try:
                q_date: date = idx.date() if hasattr(idx, "date") else idx
                period_str = format_period(q_date.year, quarter_from_date(q_date))
                results.append(
                    EarningsRecord(
                        symbol=sym,
                        period=period_str,
                        fiscal_date=q_date,
                        eps_actual=_safe_float(row.get("epsActual")),
                        eps_estimate=_safe_float(row.get("epsEstimate")),
                        eps_surprise=_safe_float(row.get("epsDifference")),
                        revenue_actual=None,
                        revenue_estimate=None,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping earnings row for %s at %s: %s", symbol, idx, exc)
                continue

        return results

    # -------------------------------------------------------------------
    # get_insider_trades — Type A
    # -------------------------------------------------------------------

    def get_insider_trades(
        self,
        symbol: str,
        since: date | None = None,
    ) -> list[InsiderTrade]:
        """Fetch insider transactions from yfinance insider_transactions DataFrame."""
        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            df = ticker.insider_transactions
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance insider trades failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if df is None or df.empty:
            return []

        results: list[InsiderTrade] = []
        for _, row in df.iterrows():
            try:
                raw_date = row.get("Start Date")
                if raw_date is None:
                    continue
                if isinstance(raw_date, str):
                    trade_d = date.fromisoformat(raw_date)
                elif hasattr(raw_date, "date"):
                    trade_d = raw_date.date()
                else:
                    trade_d = raw_date

                if since and trade_d < since:
                    continue

                text = str(row.get("Text") or "").lower()
                if "sale" in text:
                    trade_type = "sell"
                elif "purchase" in text or "buy" in text:
                    trade_type = "buy"
                elif "exercise" in text:
                    trade_type = "exercise"
                elif "gift" in text:
                    trade_type = "gift"
                else:
                    trade_type = "unknown"

                shares = abs(float(row.get("Shares") or 0))
                value = _safe_float(row.get("Value"))
                price_per_share = (value / shares) if (shares > 0 and value is not None) else None

                results.append(
                    InsiderTrade(
                        symbol=sym,
                        filing_date=trade_d,
                        trade_date=trade_d,
                        insider_name=str(row.get("Insider") or "Unknown"),
                        insider_title=str(row.get("Position")) or None,
                        trade_type=trade_type,
                        shares=shares,
                        price_per_share=price_per_share,
                        total_value=value,
                        shares_owned_after=None,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping insider row for %s: %s", symbol, exc)
                continue

        return results

    def get_short_interest(self, symbol: str) -> ShortInterest:
        """Fetch short interest data from yfinance ticker.info."""
        now = utc_now()
        sym = normalize_symbol(symbol)
        ticker = yf.Ticker(sym)

        try:
            info: dict[str, Any] = ticker.info or {}
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance .info failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        raw_float_pct = _safe_float(info.get("shortPercentOfFloat"))
        short_float_pct = round(raw_float_pct * 100, 4) if raw_float_pct is not None else None

        settlement_date = None
        raw_date = info.get("dateShortInterest")
        if raw_date:
            try:
                settlement_date = datetime.fromtimestamp(raw_date, tz=UTC).date()
            except (TypeError, ValueError, OSError):
                pass

        return ShortInterest(
            symbol=sym,
            short_interest=_safe_int(info.get("sharesShort")),
            short_float_pct=short_float_pct,
            days_to_cover=_safe_float(info.get("shortRatio")),
            settlement_date=settlement_date,
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # Rate-limit detection
    # -------------------------------------------------------------------

    def is_rate_limited(self, response: Any) -> bool:
        """yfinance signals rate limits via empty responses or exceptions.

        Since yfinance is an unofficial scraper, rate limits manifest as
        empty DataFrames, HTTP 429 buried in exceptions, or
        ``YFRateLimitError`` (added in newer yfinance versions).
        """
        if response is None:
            return True
        # Check for yfinance-specific rate limit errors
        if isinstance(response, Exception):
            err_str = str(response).lower()
            return "rate" in err_str or "429" in err_str or "too many" in err_str
        return False

    def cooldown_for(self, response: Any) -> float:
        """yfinance cooldown: 5 minutes (per design doc §7)."""
        return 300.0
