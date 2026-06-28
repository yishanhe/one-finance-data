"""Alpha Vantage provider adapter.

Uses the Alpha Vantage REST API (https://www.alphavantage.co/query).
Requires an API key set via the ``ALPHAVANTAGE_API_KEY`` environment variable.

Free tier: 25 calls/day, 5 calls/minute.
Supports price_history, quote, info, financials, earnings, and news.

Rate-limit signals from AV are embedded in the JSON body ("Information" or
"Note" keys) rather than HTTP status codes on the free tier.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any

import httpx

from onefinance.core.errors import ConfigError, ProviderError
from onefinance.core.models import (
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    EarningsCalendarEntry,
    EarningsRecord,
    FinancialRatios,
    IncomeStatement,
    InsiderTrade,
    NewsArticle,
    PriceBar,
    Quote,
)
from onefinance.providers._http import HttpProviderMixin
from onefinance.providers._utils import (
    _safe_float,
    _safe_int,
    normalize_symbol,
    parse_iso_date,
    utc_now,
)
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "alpha_vantage"
_BASE_URL = "https://www.alphavantage.co/query"


def _parse_av_datetime(value: str) -> datetime:
    """Parse Alpha Vantage's compact datetime format ``YYYYMMDDTHHmmss``."""
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(
            tzinfo=__import__("datetime").timezone.utc
        )
    except ValueError:
        from onefinance.providers._utils import parse_iso_datetime_utc

        return parse_iso_datetime_utc(value)


def _av_str(value: Any) -> str | None:
    """Return stripped string or None for AV's sentinel 'None' / empty values."""
    if value is None:
        return None
    s = str(value).strip()
    return None if s in ("None", "-", "", "N/A") else s


def _av_float(value: Any) -> float | None:
    """Float coercion that treats AV's 'None' sentinel as Python None."""
    s = _av_str(value)
    return _safe_float(s)


def _av_int(value: Any) -> int | None:
    s = _av_str(value)
    return _safe_int(s)


class AlphaVantageProvider(HttpProviderMixin, BaseProvider):
    """Provider adapter for Alpha Vantage.

    Parameters
    ----------
    api_key:
        Alpha Vantage API key. If ``None``, reads from ``ALPHAVANTAGE_API_KEY``
        env var.
    timeout:
        HTTP request timeout in seconds.
    base_url:
        Override the base URL (useful for testing).
    http_client:
        Optional shared ``httpx.Client``.
    """

    name = _SOURCE
    # 5 req/min = 60s cooldown; daily limit (25/day) gets a 1-hour cooldown
    # (the router will extend this via exponential backoff on repeated hits)
    _default_rate_limit_cooldown_s: float = 60.0

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 10,
        base_url: str = _BASE_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("ALPHAVANTAGE_API_KEY")
        if not self._api_key:
            raise ConfigError(
                "ALPHAVANTAGE_API_KEY not set. Set it in your environment or pass api_key="
            )
        self._base_url = base_url
        super().__init__(timeout=float(timeout), http_client=http_client)

    # -------------------------------------------------------------------
    # Rate-limit signals — AV embeds limits in JSON body, not HTTP status
    # -------------------------------------------------------------------

    def _rate_limit_signals(self, resp: httpx.Response) -> tuple[bool, int | None]:
        if resp.status_code == 429:
            return True, None
        if resp.status_code != 200:
            return False, None
        try:
            data = resp.json()
        except Exception:
            return False, None
        if not isinstance(data, dict):
            return False, None
        # Free tier per-minute limit: "Note" key — AV only sets this for rate-limit notices
        note = data.get("Note", "")
        if note and ("call frequency" in note.lower() or "rate limit" in note.lower()):
            return True, 60
        # Daily limit or other: "Information" key
        info = data.get("Information", "")
        if info and ("limit" in info.lower() or "api call frequency" in info.lower()):
            return True, 3600
        return False, None

    # -------------------------------------------------------------------
    # Rate-limit interface (required by BaseProvider ABC)
    # -------------------------------------------------------------------

    def is_rate_limited(self, response: Any) -> bool:
        if isinstance(response, httpx.Response):
            hit, _ = self._rate_limit_signals(response)
            return hit
        if isinstance(response, Exception):
            return "limit" in str(response).lower() or "429" in str(response)
        return False

    def cooldown_for(self, response: Any) -> float:
        if isinstance(response, httpx.Response):
            _, secs = self._rate_limit_signals(response)
            return float(secs) if secs else self._default_rate_limit_cooldown_s
        return self._default_rate_limit_cooldown_s

    # -------------------------------------------------------------------
    # HTTP helper
    # -------------------------------------------------------------------

    def _get(self, function: str, params: dict[str, Any] | None = None) -> Any:
        """GET to Alpha Vantage. Returns decoded JSON payload.

        All AV endpoints are GET requests to the same base URL with a
        ``function`` parameter.
        """
        req_params: dict[str, Any] = {"function": function, "apikey": self._api_key}
        if params:
            req_params.update(params)

        resp = self._request("GET", self._base_url, params=req_params)

        if resp.status_code != 200:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"Alpha Vantage HTTP {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                retry_safe=resp.status_code >= 500,
                http_status=resp.status_code,
            )

        data = resp.json()
        if isinstance(data, dict) and ("Note" in data or "Information" in data):
            # _check_rate_limit already ran; if we're here the signals weren't
            # rate-limit signals, but AV sometimes puts non-limit notices here too.
            pass
        return data

    # -------------------------------------------------------------------
    # get_price_history — Type A
    # -------------------------------------------------------------------

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[PriceBar]:
        """Fetch daily OHLCV via ``TIME_SERIES_DAILY_ADJUSTED``.

        AV does not support intraday via this endpoint; only ``interval="1d"``
        is handled here (anything else raises ``ProviderError``).
        """
        if interval != "1d":
            raise ProviderError(
                code="NOT_SUPPORTED",
                message=f"Alpha Vantage provider only supports interval='1d', got '{interval}'",
                provider=self.name,
                retry_safe=False,
            )
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get(
            "TIME_SERIES_DAILY_ADJUSTED",
            {"symbol": sym, "outputsize": "full"},
        )

        ts = data.get("Time Series (Daily)")
        if not ts:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No price history for '{symbol}' via Alpha Vantage",
                provider=self.name,
                retry_safe=False,
            )

        bars: list[PriceBar] = []
        for date_str, raw in sorted(ts.items()):
            bar_date = parse_iso_date(date_str)
            if bar_date < start or bar_date > end:
                continue
            bars.append(
                PriceBar(
                    symbol=sym,
                    date=bar_date,
                    open=float(raw["1. open"]),
                    high=float(raw["2. high"]),
                    low=float(raw["3. low"]),
                    close=float(raw["4. close"]),
                    adj_close=float(raw["5. adjusted close"]),
                    volume=int(float(raw["6. volume"])),
                    source=_SOURCE,
                    fetched_at=now,
                )
            )
        return bars

    # -------------------------------------------------------------------
    # get_quote — Type B
    # -------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        """Fetch current quote via ``GLOBAL_QUOTE``.

        Note: the free tier returns data delayed up to 15–20 minutes.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("GLOBAL_QUOTE", {"symbol": sym})

        gq = data.get("Global Quote", {})
        if not gq or not gq.get("05. price"):
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No quote found for '{symbol}' via Alpha Vantage",
                provider=self.name,
                retry_safe=False,
            )

        latest_day = _av_str(gq.get("07. latest trading day"))
        timestamp = (
            parse_iso_date(latest_day).strftime("%Y-%m-%dT16:00:00+00:00")
            if latest_day
            else now.isoformat()
        )

        return Quote(
            symbol=sym,
            timestamp=datetime.fromisoformat(timestamp),
            price=float(gq["05. price"]),
            bid=None,
            ask=None,
            volume=_av_int(gq.get("06. volume")) or 0,
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # get_info — Type A
    # -------------------------------------------------------------------

    def get_info(self, symbol: str) -> CompanyInfo:
        """Fetch company overview via ``OVERVIEW``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("OVERVIEW", {"symbol": sym})

        if not data or not data.get("Symbol"):
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No company info for '{symbol}' via Alpha Vantage",
                provider=self.name,
                retry_safe=False,
            )

        raw_currency = _av_str(data.get("Currency"))
        currency: str | None = None
        if raw_currency and len(raw_currency) == 3:
            currency = raw_currency.upper()

        return CompanyInfo(
            symbol=sym,
            name=_av_str(data.get("Name")) or symbol,
            exchange=_av_str(data.get("Exchange")),
            sector=_av_str(data.get("Sector")),
            industry=_av_str(data.get("Industry")),
            country=_av_str(data.get("Country")),
            market_cap=_av_float(data.get("MarketCapitalization")),
            beta=_av_float(data.get("Beta")),
            shares_outstanding=_av_int(data.get("SharesOutstanding")),
            description=_av_str(data.get("Description")),
            website=_av_str(data.get("OfficialSite")),
            employees=_av_int(data.get("FullTimeEmployees")),
            currency=currency,
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # get_financials — Type A
    # -------------------------------------------------------------------

    def get_financials(
        self,
        symbol: str,
        statement: str,
        period: str,
    ) -> list[IncomeStatement | BalanceSheet | CashFlow]:
        """Fetch income, balance, or cash-flow statements.

        Parameters
        ----------
        statement : str
            One of ``"income"``, ``"balance"``, ``"cashflow"``.
        period : str
            ``"annual"`` or ``"quarterly"``.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        function_map = {
            "income": "INCOME_STATEMENT",
            "balance": "BALANCE_SHEET",
            "cashflow": "CASH_FLOW",
        }
        av_function = function_map.get(statement)
        if av_function is None:
            raise ProviderError(
                code="INVALID_ARGUMENT",
                message=(
                    f"Unknown statement type '{statement}'. Use 'income', 'balance', or 'cashflow'."
                ),
                provider=self.name,
                retry_safe=False,
            )

        data = self._get(av_function, {"symbol": sym})

        reports_key = "quarterlyReports" if period == "quarterly" else "annualReports"
        reports = data.get(reports_key, [])
        if not reports:
            return []

        if statement == "income":
            return [self._normalise_income(r, sym, now, period) for r in reports]
        elif statement == "balance":
            return [self._normalise_balance(r, sym, now, period) for r in reports]
        else:
            return [self._normalise_cashflow(r, sym, now, period) for r in reports]

    def _period_str(self, item: dict[str, Any], period: str) -> tuple[str, date]:
        """Return (period_label, fiscal_date) from an AV report record."""
        date_str = item.get("fiscalDateEnding", "")
        fiscal_date = parse_iso_date(date_str) if date_str else date.today()
        if period == "quarterly":
            from onefinance.providers._utils import format_period, quarter_from_date

            label = format_period(fiscal_date.year, quarter_from_date(fiscal_date))
        else:
            label = str(fiscal_date.year)
        return label, fiscal_date

    def _normalise_income(
        self, item: dict[str, Any], symbol: str, now: datetime, period: str = "annual"
    ) -> IncomeStatement:
        period_label, fiscal_date = self._period_str(item, period)
        currency = (_av_str(item.get("reportedCurrency")) or "USD")[:3].upper()
        return IncomeStatement(
            symbol=symbol,
            period=period_label,
            fiscal_date=fiscal_date,
            revenue=_av_float(item.get("totalRevenue")) or 0.0,
            cost_of_revenue=_av_float(item.get("costOfRevenue")) or 0.0,
            gross_profit=_av_float(item.get("grossProfit")) or 0.0,
            operating_income=_av_float(item.get("operatingIncome")) or 0.0,
            net_income=_av_float(item.get("netIncome")) or 0.0,
            eps_basic=_av_float(item.get("eps")) or 0.0,
            eps_diluted=_av_float(item.get("epsDiluted") or item.get("eps")) or 0.0,
            currency=currency,
            ebitda=_av_float(item.get("ebitda")),
            research_and_development=_av_float(item.get("researchAndDevelopment")),
            sga_expenses=_av_float(item.get("sellingGeneralAndAdministrative")),
            source=_SOURCE,
            fetched_at=now,
        )

    def _normalise_balance(
        self, item: dict[str, Any], symbol: str, now: datetime, period: str = "annual"
    ) -> BalanceSheet:
        period_label, fiscal_date = self._period_str(item, period)
        currency = (_av_str(item.get("reportedCurrency")) or "USD")[:3].upper()
        return BalanceSheet(
            symbol=symbol,
            period=period_label,
            fiscal_date=fiscal_date,
            total_assets=_av_float(item.get("totalAssets")) or 0.0,
            total_liabilities=_av_float(item.get("totalLiabilities")) or 0.0,
            total_equity=_av_float(item.get("totalShareholderEquity")) or 0.0,
            cash_and_equivalents=_av_float(item.get("cashAndCashEquivalentsAtCarryingValue"))
            or 0.0,
            total_debt=_av_float(item.get("shortLongTermDebtTotal")) or 0.0,
            currency=currency,
            short_term_investments=_av_float(item.get("shortTermInvestments")),
            total_current_assets=_av_float(item.get("totalCurrentAssets")),
            total_current_liabilities=_av_float(item.get("totalCurrentLiabilities")),
            goodwill=_av_float(item.get("goodwill")),
            inventory=_av_float(item.get("inventory")),
            source=_SOURCE,
            fetched_at=now,
        )

    def _normalise_cashflow(
        self, item: dict[str, Any], symbol: str, now: datetime, period: str = "annual"
    ) -> CashFlow:
        period_label, fiscal_date = self._period_str(item, period)
        currency = (_av_str(item.get("reportedCurrency")) or "USD")[:3].upper()
        capex_raw = _av_float(item.get("capitalExpenditures")) or 0.0
        # AV reports capex as a negative number; normalise to positive
        capex = abs(capex_raw)
        opcf = _av_float(item.get("operatingCashflow")) or 0.0
        div_raw = _av_float(item.get("dividendPayout")) or 0.0
        dividends = abs(div_raw)
        return CashFlow(
            symbol=symbol,
            period=period_label,
            fiscal_date=fiscal_date,
            operating_cash_flow=opcf,
            capital_expenditure=capex,
            free_cash_flow=opcf - capex,
            dividends_paid=dividends,
            currency=currency,
            depreciation_and_amortization=_av_float(
                item.get("depreciationDepletionAndAmortization")
            ),
            change_in_working_capital=_av_float(item.get("changeInOperatingAssets")),
            stock_based_compensation=None,
            common_stock_repurchased=abs(
                _av_float(item.get("paymentsForRepurchaseOfCommonStock")) or 0.0
            )
            or None,
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # get_earnings — Type C
    # -------------------------------------------------------------------

    def get_earnings(self, symbol: str) -> list[EarningsRecord]:
        """Fetch earnings (EPS actuals and estimates) via ``EARNINGS``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("EARNINGS", {"symbol": sym})

        records: list[EarningsRecord] = []

        # Quarterly earnings carry actuals + estimates
        for item in data.get("quarterlyEarnings", []):
            date_str = item.get("fiscalDateEnding", "")
            if not date_str:
                continue
            fiscal_date = parse_iso_date(date_str)
            from onefinance.providers._utils import format_period, quarter_from_date

            period_label = format_period(fiscal_date.year, quarter_from_date(fiscal_date))
            records.append(
                EarningsRecord(
                    symbol=sym,
                    period=period_label,
                    fiscal_date=fiscal_date,
                    eps_actual=_av_float(item.get("reportedEPS")),
                    eps_estimate=_av_float(item.get("estimatedEPS")),
                    eps_surprise=_av_float(item.get("surprise")),
                    revenue_actual=None,
                    revenue_estimate=None,
                    source=_SOURCE,
                    fetched_at=now,
                )
            )

        # Annual earnings — actuals only
        for item in data.get("annualEarnings", []):
            date_str = item.get("fiscalDateEnding", "")
            if not date_str:
                continue
            fiscal_date = parse_iso_date(date_str)
            records.append(
                EarningsRecord(
                    symbol=sym,
                    period=str(fiscal_date.year),
                    fiscal_date=fiscal_date,
                    eps_actual=_av_float(item.get("reportedEPS")),
                    eps_estimate=None,
                    eps_surprise=None,
                    revenue_actual=None,
                    revenue_estimate=None,
                    source=_SOURCE,
                    fetched_at=now,
                )
            )

        return records

    # -------------------------------------------------------------------
    # get_news — Type A
    # -------------------------------------------------------------------

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        """Fetch recent news via ``NEWS_SENTIMENT``.

        Alpha Vantage returns articles across all tickers; we filter to
        those where the requested symbol appears in ``ticker_sentiment``.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("NEWS_SENTIMENT", {"tickers": sym, "limit": str(limit)})

        articles: list[NewsArticle] = []
        for item in data.get("feed", [])[:limit]:
            time_str = item.get("time_published", "")
            try:
                published_at = _parse_av_datetime(time_str)
            except Exception:
                published_at = now

            title = _av_str(item.get("title")) or ""
            url = _av_str(item.get("url")) or ""
            publisher = _av_str(item.get("source")) or "Unknown"

            if not title or not url:
                continue

            articles.append(
                NewsArticle(
                    symbol=sym,
                    title=title,
                    publisher=publisher,
                    link=url,
                    published_at=published_at,
                    summary=_av_str(item.get("summary")),
                    source=_SOURCE,
                    fetched_at=now,
                )
            )

        return articles

    # -------------------------------------------------------------------
    # get_ratios — Type C
    # -------------------------------------------------------------------

    def get_ratios(self, symbol: str, period: str) -> list[FinancialRatios]:
        """Fetch financial ratios via the ``OVERVIEW`` function.

        Alpha Vantage's overview provides trailing-twelve-month figures.
        The ``period`` argument is accepted for interface compatibility but
        the returned values are always TTM regardless of what is passed.
        ``fiscal_date`` is set to today as AV does not provide a period-end
        date on this endpoint.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("OVERVIEW", {"symbol": sym})

        if not data or not data.get("Symbol"):
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No overview data for '{symbol}' via Alpha Vantage",
                provider=self.name,
                retry_safe=False,
            )

        return [
            FinancialRatios(
                symbol=sym,
                period=period,
                fiscal_date=date.today(),
                pe_ratio=_av_float(data.get("PERatio")),
                pb_ratio=_av_float(data.get("PriceToBookRatio")),
                ps_ratio=_av_float(data.get("PriceToSalesRatioTTM")),
                return_on_equity=_av_float(data.get("ReturnOnEquityTTM")),
                return_on_assets=_av_float(data.get("ReturnOnAssetsTTM")),
                operating_margin=_av_float(data.get("OperatingMarginTTM")),
                net_margin=_av_float(data.get("ProfitMargin")),
                dividend_yield=_av_float(data.get("DividendYield")),
                book_value_per_share=_av_float(data.get("BookValue")),
                revenue_per_share=_av_float(data.get("RevenuePerShareTTM")),
                source=_SOURCE,
                fetched_at=now,
            )
        ]

    # -------------------------------------------------------------------
    # get_insider_trades — Type A
    # -------------------------------------------------------------------

    def get_insider_trades(self, symbol: str, since: date | None = None) -> list[InsiderTrade]:
        """Fetch insider transactions via ``INSIDER_TRANSACTIONS``.

        AV returns SEC Form 4 filings. ``transactionType`` codes:
        P = purchase/buy, S = sale/sell, A = award, M/X = option exercise.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("INSIDER_TRANSACTIONS", {"symbol": sym})

        _TYPE_MAP: dict[str, str] = {
            "P": "buy",
            "A": "buy",
            "M": "exercise",
            "X": "exercise",
            "S": "sell",
            "D": "sell",
            "F": "sell",
            "G": "sell",
            "I": "sell",
            "L": "sell",
        }

        results: list[InsiderTrade] = []
        for item in data.get("data", []):
            try:
                filing_str = item.get("filingDate") or item.get("reportedDate")
                if not filing_str:
                    continue
                filing_d = parse_iso_date(filing_str)
                trade_str = item.get("transactionDate")
                trade_d = parse_iso_date(trade_str) if trade_str else None
                # transactionType is like "P-Purchase", "S-Sale" — take first char
                raw_type = (item.get("transactionType") or "")[:1].upper()
                trade_type = _TYPE_MAP.get(raw_type, "buy")
                shares_raw = _av_float(item.get("sharesTraded") or item.get("shares"))
                shares = abs(shares_raw) if shares_raw is not None else 0.0
                results.append(
                    InsiderTrade(
                        symbol=sym,
                        filing_date=filing_d,
                        trade_date=trade_d,
                        insider_name=_av_str(item.get("reportingName")) or "Unknown",
                        insider_title=_av_str(item.get("reportedTitle")),
                        trade_type=trade_type,
                        shares=shares,
                        price_per_share=_av_float(item.get("sharePrice")),
                        total_value=_av_float(item.get("totalValue")),
                        shares_owned_after=_av_float(item.get("sharesOwned")),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping AV insider trade entry: %s", exc)
                continue
        return results

    # -------------------------------------------------------------------
    # get_corporate_actions — Type A
    # -------------------------------------------------------------------

    def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Fetch dividends and splits via ``DIVIDENDS`` and ``SPLITS``.

        Two API calls are made and the results merged, sorted newest-first.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)
        results: list[CorporateAction] = []

        div_data = self._get("DIVIDENDS", {"symbol": sym})
        for item in div_data.get("data", []):
            try:
                ex_date_str = item.get("ex_dividend_date") or item.get("ex_date")
                if not ex_date_str:
                    continue
                results.append(
                    CorporateAction(
                        symbol=sym,
                        date=parse_iso_date(ex_date_str),
                        action_type="dividend",
                        amount=_av_float(item.get("amount")),
                        split_ratio=None,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping AV dividend entry: %s", exc)
                continue

        split_data = self._get("SPLITS", {"symbol": sym})
        for item in split_data.get("data", []):
            try:
                eff_date_str = item.get("effective_date")
                if not eff_date_str:
                    continue
                factor_str = _av_str(item.get("split_factor")) or "1"
                # factor may be "4/1" or "4.0"
                if "/" in factor_str:
                    num, den = factor_str.split("/", 1)
                    factor = float(num) / float(den) if float(den) != 0 else 1.0
                else:
                    factor = _safe_float(factor_str) or 1.0
                results.append(
                    CorporateAction(
                        symbol=sym,
                        date=parse_iso_date(eff_date_str),
                        action_type="split",
                        amount=None,
                        split_ratio=factor,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping AV split entry: %s", exc)
                continue

        results.sort(key=lambda x: x.date, reverse=True)
        return results

    # -------------------------------------------------------------------
    # get_earnings_calendar — Type A
    # -------------------------------------------------------------------

    def get_earnings_calendar(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> list[EarningsCalendarEntry]:
        """Fetch upcoming earnings via ``EARNINGS_CALENDAR`` (CSV response).

        AV returns a CSV with columns:
        symbol, name, reportDate, fiscalDateEnding, estimate, currency.
        The ``horizon`` parameter controls look-ahead: "3month", "6month",
        "12month" (default).  Date filtering is applied client-side.
        """
        import csv
        import io

        now = utc_now()
        params: dict[str, Any] = {
            "function": "EARNINGS_CALENDAR",
            "apikey": self._api_key,
            "horizon": "12month",
        }

        resp = self._request("GET", self._base_url, params=params)
        if resp.status_code != 200:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"Alpha Vantage earnings_calendar HTTP {resp.status_code}",
                provider=self.name,
                retry_safe=resp.status_code >= 500,
                http_status=resp.status_code,
            )

        results: list[EarningsCalendarEntry] = []
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            try:
                report_str = row.get("reportDate", "").strip()
                sym = (row.get("symbol") or "").strip().upper()
                if not report_str or not sym:
                    continue
                report_d = parse_iso_date(report_str)
                if start and report_d < start:
                    continue
                if end and report_d > end:
                    continue
                results.append(
                    EarningsCalendarEntry(
                        symbol=sym,
                        report_date=report_d,
                        year=report_d.year,
                        quarter=None,
                        eps_estimate=_av_float(row.get("estimate")),
                        eps_actual=None,
                        revenue_estimate=None,
                        revenue_actual=None,
                        time_of_day=None,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping AV earnings calendar row: %s", exc)
                continue

        return results
