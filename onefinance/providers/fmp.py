"""FMP (Financial Modeling Prep) provider adapter.

Uses the ``/stable/`` API endpoints (post-August 2025 migration).
Requires an API key set via the ``FMP_API_KEY`` environment variable.

FMP is the primary paid provider — best fundamentals coverage,
realtime quotes on paid plans. Free tier: 250 calls/day.

See design doc §6, §7, §9 for the provider contract.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime
from typing import Any

import httpx

from onefinance.core.errors import (
    ConfigError,
    NotSupportedError,
    ProviderError,
)
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    DCFValuation,
    EarningsCalendarEntry,
    EarningsRecord,
    EconomicEvent,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    MarketSentiment,
    NewsArticle,
    PeerCompany,
    PriceBar,
    Quote,
    ScreenerResult,
    SectorInfo,
    ShortInterest,
)
from onefinance.providers._http import HttpProviderMixin
from onefinance.providers._utils import (
    _safe_float,
    _safe_int,
    change_pct_from_prev_close,
    format_period,
    normalize_symbol,
    parse_iso_date,
    parse_iso_datetime_utc,
    quarter_from_date,
    utc_now,
)
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "fmp"
_BASE_URL = "https://financialmodelingprep.com/stable"


class FMPProvider(HttpProviderMixin, BaseProvider):
    """Provider adapter for Financial Modeling Prep (stable API).

    Parameters
    ----------
    api_key:
        FMP API key. If ``None``, reads from ``FMP_API_KEY`` env var.
    timeout:
        HTTP request timeout in seconds.
    base_url:
        Override the base URL (useful for testing).
    http_client:
        Optional shared ``httpx.Client`` (used for testing or connection pooling).
    """

    name = _SOURCE
    _default_rate_limit_cooldown_s: float = 3600.0

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 10,
        base_url: str = _BASE_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("FMP_API_KEY")
        if not self._api_key:
            raise ConfigError("FMP_API_KEY not set. Set it in your environment or pass api_key=")
        self._base_url = base_url
        super().__init__(timeout=float(timeout), http_client=http_client)

    # -------------------------------------------------------------------
    # Rate-limit signals override — adds FMP quirks
    # -------------------------------------------------------------------

    def _rate_limit_signals(self, resp: httpx.Response) -> tuple[bool, int | None]:
        if resp.status_code == 429:
            return True, None
        if resp.status_code != 200:
            body = resp.text
            if "Limit Reach" in body or "limit reach" in body.lower():
                return True, None
            return False, None
        try:
            data = resp.json()
        except Exception:
            return False, None
        if isinstance(data, dict):
            msg = data.get("Error Message") or data.get("error")
            if msg and "limit" in str(msg).lower():
                return True, None
        return False, None

    # -------------------------------------------------------------------
    # HTTP helper
    # -------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Authenticated GET to FMP. Returns the decoded JSON payload.

        Rate-limit detection is delegated to the mixin via
        :meth:`_rate_limit_signals`; non-rate-limit failures raise
        :class:`ProviderError` with a stable code.
        """
        url = f"{self._base_url}/{path}"
        req_params = dict(params or {})
        req_params["apikey"] = self._api_key

        resp = self._request("GET", url, params=req_params)

        if resp.status_code == 402:
            raise NotSupportedError(self.name, path, http_status=402)
        self._raise_for_status(resp)

        data = resp.json()
        if isinstance(data, dict):
            error_msg = data.get("Error Message") or data.get("error")
            if error_msg:
                raise ProviderError(
                    code="NETWORK_ERROR",
                    message=f"FMP error: {error_msg}",
                    provider=self.name,
                    retry_safe=False,
                )
        return data

    # -------------------------------------------------------------------
    # get_price_history — Type A
    # -------------------------------------------------------------------

    _INTRADAY_RES_MAP = {
        "1m": "1min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "60m": "1hour",
        "1h": "1hour",
    }

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[PriceBar]:
        """Fetch OHLCV bars via FMP."""
        sym = normalize_symbol(symbol)
        raw = self._fetch_price_history_raw(sym, start, end, interval)
        bars = self._parse_price_bars(raw, sym)
        bars.reverse()
        return bars

    def _fetch_price_history_raw(
        self, sym: str, start: date, end: date, interval: str
    ) -> list[dict[str, Any]]:
        if interval in ("1d", "1wk", "1mo"):
            data = self._get(
                "historical-price-eod/full",
                params={"symbol": sym, "from": start.isoformat(), "to": end.isoformat()},
            )
        else:
            res = self._INTRADAY_RES_MAP.get(interval, "1hour")
            data = self._get(
                f"historical-chart/{res}/{sym}",
                params={"from": start.isoformat(), "to": end.isoformat()},
            )
        return data if isinstance(data, list) else []

    def _parse_price_bars(self, raw: list[dict[str, Any]], sym: str) -> list[PriceBar]:
        now = utc_now()
        bars: list[PriceBar] = []
        for item in raw:
            try:
                bars.append(self._parse_price_bar(item, sym, now))
            except Exception as exc:
                logger.warning("Skipping FMP bar for %s: %s", sym, exc)
        return bars

    @staticmethod
    def _parse_price_bar(item: dict[str, Any], sym: str, now: datetime) -> PriceBar:
        dt_str = item["date"]
        bar_ts: datetime | None
        if len(dt_str) > 10:
            bar_ts = parse_iso_datetime_utc(dt_str)
            bar_date = bar_ts.date()
        else:
            bar_date = parse_iso_date(dt_str)
            bar_ts = None
        return PriceBar(
            symbol=sym,
            date=bar_date,
            timestamp=bar_ts,
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
            adj_close=float(item.get("adjClose", item["close"])),
            volume=int(item["volume"]),
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # get_quote — Type B
    # -------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        """Fetch realtime quote via ``/stable/quote``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("quote", params={"symbol": sym})

        if not data:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No quote found for '{symbol}' via FMP",
                provider=self.name,
                retry_safe=False,
            )

        item = data[0] if isinstance(data, list) else data
        price = float(item["price"])
        prev_close = _safe_float(item.get("previousClose"))

        return Quote(
            symbol=sym,
            timestamp=datetime.fromtimestamp(item["timestamp"], tz=UTC)
            if item.get("timestamp")
            else now,
            price=price,
            bid=None,
            ask=None,
            volume=int(item.get("volume", 0)),
            prev_close=prev_close,
            change_pct=change_pct_from_prev_close(price, prev_close),
            market_cap=_safe_float(item.get("marketCap")),
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # get_info — Type A
    # -------------------------------------------------------------------

    def get_info(self, symbol: str) -> CompanyInfo:
        """Fetch company profile via ``/stable/profile``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("profile", params={"symbol": sym})

        if not data:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No profile found for '{symbol}' via FMP",
                provider=self.name,
                retry_safe=False,
            )

        item = data[0] if isinstance(data, list) else data

        raw_currency = item.get("currency")
        currency: str | None = None
        if raw_currency and isinstance(raw_currency, str) and len(raw_currency) == 3:
            currency = raw_currency.upper()

        return CompanyInfo(
            symbol=sym,
            name=item.get("companyName") or symbol,
            exchange=item.get("exchange"),
            sector=item.get("sector"),
            industry=item.get("industry"),
            country=item.get("country"),
            market_cap=_safe_float(item.get("mktCap") or item.get("marketCap")),
            beta=_safe_float(item.get("beta")),
            shares_outstanding=_safe_int(item.get("sharesOutstanding")),
            description=item.get("description"),
            website=item.get("website"),
            employees=_safe_int(item.get("fullTimeEmployees")),
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
        """Fetch financial statements via FMP stable API.

        Parameters
        ----------
        statement : str
            One of ``"income"``, ``"balance"``, ``"cashflow"``.
        period : str
            ``"annual"`` or ``"quarterly"``.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        endpoint_map = {
            "income": "income-statement",
            "balance": "balance-sheet-statement",
            "cashflow": "cash-flow-statement",
        }

        api_endpoint = endpoint_map.get(statement)
        if api_endpoint is None:
            raise ProviderError(
                code="INVALID_ARGUMENT",
                message=(
                    f"Unknown statement type: '{statement}'. "
                    "Use 'income', 'balance', or 'cashflow'."
                ),
                provider=self.name,
                retry_safe=False,
            )

        fmp_period = "quarter" if period == "quarterly" else "annual"

        data = self._get(
            api_endpoint,
            params={"symbol": sym, "period": fmp_period, "limit": 5},
        )

        if not data or not isinstance(data, list):
            return []

        if statement == "income":
            return [self._normalise_income(item, sym, now) for item in data]
        elif statement == "balance":
            return [self._normalise_balance(item, sym, now) for item in data]
        else:
            return [self._normalise_cashflow(item, sym, now) for item in data]

    def _normalise_income(
        self, item: dict[str, Any], symbol: str, now: datetime
    ) -> IncomeStatement:
        fiscal_year = item.get("fiscalYear", "")
        fmp_period = item.get("period", "FY")
        period_str = format_period(fiscal_year, fmp_period)

        return IncomeStatement(
            symbol=symbol,
            period=period_str,
            fiscal_date=parse_iso_date(item["date"]),
            revenue=float(item.get("revenue", 0)),
            cost_of_revenue=float(item.get("costOfRevenue", 0)),
            gross_profit=float(item.get("grossProfit", 0)),
            operating_income=float(item.get("operatingIncome", 0)),
            net_income=float(item.get("netIncome", 0)),
            eps_basic=float(item.get("eps", 0)),
            eps_diluted=float(item.get("epsDiluted", item.get("eps", 0))),
            currency=item.get("reportedCurrency", "USD")[:3].upper(),
            source=_SOURCE,
            fetched_at=now,
            ebitda=_safe_float(item.get("ebitda")),
            research_and_development=_safe_float(item.get("researchAndDevelopmentExpenses")),
            sga_expenses=_safe_float(item.get("sellingGeneralAndAdministrativeExpenses")),
        )

    def _normalise_balance(self, item: dict[str, Any], symbol: str, now: datetime) -> BalanceSheet:
        fiscal_year = item.get("fiscalYear", "")
        fmp_period = item.get("period", "FY")
        period_str = format_period(fiscal_year, fmp_period)

        return BalanceSheet(
            symbol=symbol,
            period=period_str,
            fiscal_date=parse_iso_date(item["date"]),
            total_assets=float(item.get("totalAssets", 0)),
            total_liabilities=float(item.get("totalLiabilities", 0)),
            total_equity=float(item.get("totalStockholdersEquity", 0)),
            cash_and_equivalents=float(item.get("cashAndCashEquivalents", 0)),
            total_debt=float(item.get("totalDebt", 0)),
            currency=item.get("reportedCurrency", "USD")[:3].upper(),
            source=_SOURCE,
            fetched_at=now,
            short_term_investments=_safe_float(item.get("shortTermInvestments")),
            total_current_assets=_safe_float(item.get("totalCurrentAssets")),
            total_current_liabilities=_safe_float(item.get("totalCurrentLiabilities")),
            net_debt=_safe_float(item.get("netDebt")),
            goodwill=_safe_float(item.get("goodwill")),
            inventory=_safe_float(item.get("inventory")),
        )

    def _normalise_cashflow(self, item: dict[str, Any], symbol: str, now: datetime) -> CashFlow:
        fiscal_year = item.get("fiscalYear", "")
        fmp_period = item.get("period", "FY")
        period_str = format_period(fiscal_year, fmp_period)

        return CashFlow(
            symbol=symbol,
            period=period_str,
            fiscal_date=parse_iso_date(item["date"]),
            operating_cash_flow=float(item.get("operatingCashFlow", 0)),
            capital_expenditure=float(item.get("capitalExpenditure", 0)),
            free_cash_flow=float(item.get("freeCashFlow", 0)),
            dividends_paid=float(item.get("dividendsPaid", 0)),
            currency=item.get("reportedCurrency", "USD")[:3].upper(),
            source=_SOURCE,
            fetched_at=now,
            depreciation_and_amortization=_safe_float(item.get("depreciationAndAmortization")),
            change_in_working_capital=_safe_float(item.get("changeInWorkingCapital")),
            stock_based_compensation=_safe_float(item.get("stockBasedCompensation")),
            common_stock_repurchased=_safe_float(item.get("commonStockRepurchased")),
        )

    # -------------------------------------------------------------------
    # get_ratios — Type C
    # -------------------------------------------------------------------

    def get_ratios(
        self,
        symbol: str,
        period: str,
    ) -> list[FinancialRatios]:
        """Fetch financial ratios via ``/stable/ratios``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        fmp_period = "quarter" if period == "quarterly" else "annual"

        data = self._get(
            "ratios",
            params={"symbol": sym, "period": fmp_period, "limit": 5},
        )

        if not data or not isinstance(data, list):
            return []

        results: list[FinancialRatios] = []
        for item in data:
            fiscal_year = item.get("fiscalYear", "")
            fmp_period_label = item.get("period", "FY")
            period_str = format_period(fiscal_year, fmp_period_label)

            results.append(
                FinancialRatios(
                    symbol=sym,
                    period=period_str,
                    fiscal_date=parse_iso_date(item["date"]),
                    pe_ratio=_safe_float(item.get("priceEarningsRatio")),
                    pb_ratio=_safe_float(item.get("priceToBookRatio")),
                    ps_ratio=_safe_float(item.get("priceToSalesRatio")),
                    debt_to_equity=_safe_float(item.get("debtEquityRatio")),
                    current_ratio=_safe_float(item.get("currentRatio")),
                    return_on_equity=_safe_float(item.get("returnOnEquity")),
                    return_on_assets=_safe_float(item.get("returnOnAssets")),
                    gross_margin=_safe_float(item.get("grossProfitMargin")),
                    operating_margin=_safe_float(item.get("operatingProfitMargin")),
                    net_margin=_safe_float(item.get("netProfitMargin")),
                    dividend_yield=_safe_float(item.get("dividendYield")),
                    quick_ratio=_safe_float(item.get("quickRatio")),
                    interest_coverage=_safe_float(item.get("interestCoverage")),
                    enterprise_value=_safe_float(item.get("enterpriseValue")),
                    roic=_safe_float(item.get("returnOnCapitalEmployed")),
                    ev_to_ebitda=_safe_float(item.get("enterpriseValueOverEBITDA")),
                    ev_to_sales=_safe_float(item.get("evToSales")),
                    free_cash_flow_yield=_safe_float(item.get("freeCashFlowYield")),
                    book_value_per_share=_safe_float(item.get("bookValuePerShare")),
                    revenue_per_share=_safe_float(item.get("revenuePerShare")),
                    source=_SOURCE,
                    fetched_at=now,
                )
            )

        return results

    # -------------------------------------------------------------------
    # get_earnings — Type C
    # -------------------------------------------------------------------

    def get_earnings(self, symbol: str) -> list[EarningsRecord]:
        """Fetch earnings history via ``/stable/earnings``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("earnings", params={"symbol": sym})

        if not data or not isinstance(data, list):
            return []

        results: list[EarningsRecord] = []
        for item in data:
            item_date = item.get("date")
            if not item_date:
                continue

            d = parse_iso_date(item_date)
            period_str = format_period(d.year, quarter_from_date(d))

            results.append(
                EarningsRecord(
                    symbol=sym,
                    period=period_str,
                    fiscal_date=d,
                    eps_actual=_safe_float(item.get("epsActual")),
                    eps_estimate=_safe_float(item.get("epsEstimated")),
                    eps_surprise=(
                        round(
                            float(item["epsActual"]) - float(item["epsEstimated"]),
                            4,
                        )
                        if item.get("epsActual") is not None
                        and item.get("epsEstimated") is not None
                        else None
                    ),
                    revenue_actual=_safe_float(item.get("revenueActual")),
                    revenue_estimate=_safe_float(item.get("revenueEstimated")),
                    source=_SOURCE,
                    fetched_at=now,
                )
            )

        return results

    # -------------------------------------------------------------------
    # get_insider_trades — Type A
    # -------------------------------------------------------------------

    def get_insider_trades(
        self,
        symbol: str,
        since: date | None = None,
    ) -> list[InsiderTrade]:
        """Fetch insider trades via ``/stable/insider-trading``.

        Note: This endpoint may require a paid FMP plan.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        params: dict[str, Any] = {"symbol": sym, "limit": 100}

        data = self._get("insider-trading", params=params)

        if not data or not isinstance(data, list):
            return []

        results: list[InsiderTrade] = []
        for item in data:
            filing_date_str = item.get("filingDate")
            if not filing_date_str:
                continue

            filing_d = parse_iso_date(filing_date_str[:10])

            if since and filing_d < since:
                continue

            trade_date_str = item.get("transactionDate")
            trade_d = parse_iso_date(trade_date_str[:10]) if trade_date_str else None

            # Map FMP transaction types
            raw_type = (item.get("transactionType") or "").lower()
            if "purchase" in raw_type or "buy" in raw_type:
                trade_type = "buy"
            elif "sale" in raw_type or "sell" in raw_type:
                trade_type = "sell"
            elif "exercise" in raw_type or "option" in raw_type:
                trade_type = "exercise"
            else:
                trade_type = raw_type or "unknown"

            shares = abs(float(item.get("securitiesTransacted", 0)))
            price = _safe_float(item.get("price"))
            total_value = _safe_float(item.get("value"))

            results.append(
                InsiderTrade(
                    symbol=sym,
                    filing_date=filing_d,
                    trade_date=trade_d,
                    insider_name=item.get("reportingName") or item.get("reportingOwner", "Unknown"),
                    insider_title=item.get("typeOfOwner"),
                    trade_type=trade_type,
                    shares=shares,
                    price_per_share=price,
                    total_value=total_value,
                    shares_owned_after=_safe_float(item.get("securitiesOwned")),
                    source=_SOURCE,
                    fetched_at=now,
                )
            )

        return results

    # -------------------------------------------------------------------
    # get_dcf — Type A
    # -------------------------------------------------------------------

    def get_dcf(self, symbol: str) -> DCFValuation:
        """Fetch DCF valuation via ``/stable/discounted-cash-flow``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("discounted-cash-flow", params={"symbol": sym})

        if not data:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No DCF data found for '{symbol}' via FMP",
                provider=self.name,
                retry_safe=False,
            )

        item = data[0] if isinstance(data, list) else data

        dcf_value = item.get("dcf")
        stock_price = item.get("stockPrice") or item.get("Stock Price")
        dcf_date_str = item.get("date")

        if dcf_value is None or stock_price is None:
            raise ProviderError(
                code="SCHEMA_DRIFT",
                message=f"FMP DCF response missing required fields for '{symbol}'",
                provider=self.name,
                retry_safe=True,
            )

        return DCFValuation(
            symbol=sym,
            dcf=float(dcf_value),
            stock_price=float(stock_price),
            dcf_date=parse_iso_date(dcf_date_str) if dcf_date_str else date.today(),
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # Alternative Data
    # -------------------------------------------------------------------

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        """News endpoint not available on the current FMP plan."""
        raise NotSupportedError(self.name, "news")

    def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Fetch dividend and split history from FMP."""
        from onefinance.core.models import CorporateAction

        now = utc_now()
        sym = normalize_symbol(symbol)
        actions = []

        div_data = self._get(f"historical-price-full/stock_dividend/{sym}")
        if isinstance(div_data, dict) and "historical" in div_data:
            for d in div_data["historical"]:
                try:
                    actions.append(
                        CorporateAction(
                            symbol=sym,
                            date=parse_iso_date(d["date"]),
                            action_type="dividend",
                            amount=float(d["adjDividend"])
                            if "adjDividend" in d
                            else float(d.get("dividend", 0)),
                            source=_SOURCE,
                            fetched_at=now,
                        )
                    )
                except Exception:
                    continue

        split_data = self._get(f"historical-price-full/stock_split/{sym}")
        if isinstance(split_data, dict) and "historical" in split_data:
            for s in split_data["historical"]:
                try:
                    num = float(s.get("numerator", 0))
                    den = float(s.get("denominator", 1))
                    ratio = num / den if den != 0 else 0
                    actions.append(
                        CorporateAction(
                            symbol=sym,
                            date=parse_iso_date(s["date"]),
                            action_type="split",
                            split_ratio=ratio,
                            source=_SOURCE,
                            fetched_at=now,
                        )
                    )
                except Exception:
                    continue

        return sorted(actions, key=lambda a: a.date, reverse=True)

    def get_institutional_holders(self, symbol: str) -> list[InstitutionalHolder]:
        """Fetch top institutional holders from FMP."""
        from onefinance.core.models import InstitutionalHolder

        now = utc_now()
        sym = normalize_symbol(symbol)
        data = self._get(f"institutional-holder/{sym}")
        if not data or not isinstance(data, list):
            return []

        holders = []
        for h in data:
            try:
                holders.append(
                    InstitutionalHolder(
                        symbol=sym,
                        holder_name=h.get("holder", ""),
                        shares=int(h.get("shares", 0)),
                        value=float(h.get("marketValue", 0))
                        if h.get("marketValue") is not None
                        else None,
                        change=int(h.get("change", 0)) if h.get("change") is not None else None,
                        change_pct=float(h.get("changePercentage", 0))
                        if h.get("changePercentage") is not None
                        else None,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Failed to parse institutional holder for %s: %s", sym, exc)
                continue
        return holders

    def get_analyst_data(self, symbol: str) -> AnalystData:
        """Fetch analyst price targets and ratings from FMP."""
        from onefinance.core.models import AnalystData

        now = utc_now()
        sym = normalize_symbol(symbol)

        pt_data = self._get("price-target-consensus", params={"symbol": sym})
        pt = pt_data[0] if isinstance(pt_data, list) and pt_data else {}

        # analyst-ratings redirects (302→404) on the stable API for some accounts;
        # degrade gracefully so price-target data still gets returned.
        rt: dict[str, Any] = {}
        try:
            rating_data = self._get("analyst-ratings", params={"symbol": sym})
            rt = rating_data[0] if isinstance(rating_data, list) and rating_data else {}
        except Exception:
            pass

        if not pt and not rt:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No analyst data found for symbol '{symbol}' via FMP",
                provider=self.name,
                retry_safe=False,
            )

        return AnalystData(
            symbol=sym,
            target_high=_safe_float(pt.get("targetHigh")),
            target_low=_safe_float(pt.get("targetLow")),
            target_mean=_safe_float(pt.get("targetConsensus")),
            target_median=_safe_float(pt.get("targetMedian")),
            rating_buy=_safe_int(rt.get("analystRatingsBuy")),
            rating_hold=_safe_int(rt.get("analystRatingsHold")),
            rating_sell=_safe_int(rt.get("analystRatingsSell")),
            rating_strong_buy=_safe_int(rt.get("analystRatingsStrongBuy")),
            rating_strong_sell=_safe_int(rt.get("analystRatingsStrongSell")),
            source=_SOURCE,
            fetched_at=now,
        )

    def screen_stocks(self, query: str) -> list[ScreenerResult]:
        """Screen stocks via FMP /stock-screener.

        *query* should be a URL-encoded string like
        ``'marketCapMoreThan=1000000000&sector=Technology'``.
        """
        now = utc_now()
        from urllib.parse import parse_qsl

        params = dict(parse_qsl(query))

        data = self._get("stock-screener", params=params)
        if not data or not isinstance(data, list):
            return []

        results = []
        for item in data[:50]:  # Limit to 50
            results.append(
                ScreenerResult(
                    symbol=item.get("symbol", ""),
                    company_name=item.get("companyName"),
                    market_cap=_safe_float(item.get("marketCap")),
                    sector=item.get("sector"),
                    industry=item.get("industry"),
                    price=_safe_float(item.get("price")),
                    volume=_safe_int(item.get("volume")),
                    source=_SOURCE,
                    fetched_at=now,
                )
            )
        return results

    # -------------------------------------------------------------------
    # get_sector_overview — Type A
    # -------------------------------------------------------------------

    def get_sector_overview(self, sector: str) -> SectorInfo:
        """Fetch sector data via ``/stable/sectors``.

        FMP returns all sectors in one call; filter for the requested one.
        ``sector`` is matched case-insensitively (e.g. "technology", "Technology").
        """
        now = utc_now()

        data = self._get("sectors")
        if not data or not isinstance(data, list):
            raise ProviderError(
                code="EMPTY_RESPONSE",
                message="FMP /stable/sectors returned no data",
                provider=self.name,
                retry_safe=True,
            )

        sector_lower = sector.strip().lower()
        match: dict[str, Any] | None = None
        for item in data:
            if (item.get("sector") or "").lower() == sector_lower:
                match = item
                break

        if match is None:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"FMP sector not found: '{sector}'",
                provider=self.name,
                retry_safe=False,
            )

        ytd_raw = match.get("changesPercentage") or match.get("ytdReturn") or match.get("change")
        try:
            ytd = float(str(ytd_raw).replace("%", "").strip()) if ytd_raw is not None else None
            if ytd is not None and abs(ytd) > 1:
                ytd = ytd / 100.0  # convert percentage to decimal
        except (ValueError, TypeError):
            ytd = None

        return SectorInfo(
            name=match.get("sector", sector.title()),
            market_weight=_safe_float(match.get("marketWeight")),
            ytd_return=ytd,
            top_companies=None,
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # Rate-limit detection
    # -------------------------------------------------------------------

    def is_rate_limited(self, response: Any) -> bool:
        """Check if response indicates FMP rate limit."""
        if isinstance(response, httpx.Response):
            if response.status_code == 429:
                return True
            try:
                body = response.text
                return "Limit Reach" in body or "limit reach" in body.lower()
            except Exception:
                return False
        if isinstance(response, Exception):
            err_str = str(response).lower()
            return "limit" in err_str or "429" in err_str
        return False

    def cooldown_for(self, response: Any) -> float:
        """FMP daily cap cooldown: 1 hour (per design doc §7)."""
        return 3600.0

    def get_earnings_calendar(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> list[EarningsCalendarEntry]:
        """Fetch earnings calendar from FMP ``/stable/earnings-calendar``.

        Free tier ignores ``from``/``to`` and returns the current week.
        Date parameters are passed but silently dropped by FMP on free plans.
        """
        now = utc_now()
        params: dict[str, Any] = {}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()

        data = self._get("earnings-calendar", params=params)

        if not data or not isinstance(data, list):
            return []

        results: list[EarningsCalendarEntry] = []
        for item in data:
            try:
                sym = (item.get("symbol") or "").strip().upper()
                date_str = item.get("date")
                if not sym or not date_str:
                    continue
                report_d = parse_iso_date(date_str)
                results.append(
                    EarningsCalendarEntry(
                        symbol=sym,
                        report_date=report_d,
                        year=report_d.year,
                        quarter=None,
                        eps_estimate=_safe_float(item.get("epsEstimated")),
                        eps_actual=_safe_float(item.get("epsActual")),
                        revenue_estimate=_safe_float(item.get("revenueEstimated")),
                        revenue_actual=_safe_float(item.get("revenueActual")),
                        time_of_day=None,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping FMP earnings calendar entry: %s", exc)
                continue

        return results

    def get_economic_calendar(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> list[EconomicEvent]:
        """Fetch macro economic events via FMP ``/stable/economic-calendar``.

        Returns CPI, GDP, FOMC, NFP, PMI and similar macro releases.
        Free tier may limit date-range support.
        """
        now = utc_now()
        params: dict[str, Any] = {}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()

        data = self._get("economic-calendar", params=params)

        if not data or not isinstance(data, list):
            return []

        _IMPACT_MAP = {"High": "high", "Medium": "medium", "Low": "low", "None": None}

        results: list[EconomicEvent] = []
        for item in data:
            try:
                date_str = item.get("date")
                event_name = (item.get("event") or "").strip()
                if not date_str or not event_name:
                    continue
                # FMP date may include time: "2024-01-12 08:30:00"
                date_part = date_str.split(" ")[0] if " " in date_str else date_str
                time_part = date_str.split(" ")[1][:5] if " " in date_str else None
                results.append(
                    EconomicEvent(
                        event=event_name,
                        event_date=parse_iso_date(date_part),
                        event_time=time_part,
                        country=item.get("country") or None,
                        currency=item.get("currency") or None,
                        unit=item.get("unit") or None,
                        estimate=_safe_float(item.get("estimate")),
                        actual=_safe_float(item.get("actual")),
                        previous=_safe_float(item.get("previous")),
                        impact=_IMPACT_MAP.get(item.get("impact") or "", None),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping FMP economic calendar entry: %s", exc)
                continue

        return results

    def get_forward_estimates(self, symbol: str) -> list[ForwardEstimates]:
        """Fetch consensus analyst estimates via ``/stable/analyst-estimates``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get(
            "analyst-estimates", params={"symbol": sym, "period": "annual", "limit": 10}
        )

        if not data:
            return []

        results = []
        for item in data:
            f_date = None
            if item.get("date"):
                try:
                    f_date = parse_iso_date(item["date"])
                except ValueError:
                    pass

            period = f_date.strftime("%Y-FY") if f_date else "forward"

            results.append(
                ForwardEstimates(
                    symbol=sym,
                    period=period,
                    fiscal_date=f_date,
                    eps_estimate=_safe_float(item.get("estimatedEpsAvg")),
                    revenue_estimate=_safe_float(item.get("estimatedRevenueAvg")),
                    revenue_growth=None,  # Not directly in this endpoint
                    source=_SOURCE,
                    fetched_at=now,
                )
            )

        return results

    def get_short_interest(self, symbol: str) -> ShortInterest:
        """Fetch short interest via FMP ``/v3/short-float-symbol/{symbol}``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get(f"short-float-symbol/{sym}")

        if not data or not isinstance(data, list) or len(data) == 0:
            raise ProviderError(
                code="DATA_NOT_FOUND",
                message=f"No short interest data for {symbol}",
                provider=_SOURCE,
                retry_safe=False,
            )

        item = data[0]

        def _parse_pct(val: Any) -> float | None:
            if val is None:
                return None
            try:
                return float(str(val).replace("%", "").strip())
            except (ValueError, TypeError):
                return None

        settlement_date = None
        date_str = item.get("date")
        if date_str:
            try:
                settlement_date = parse_iso_date(date_str)
            except (ValueError, TypeError):
                pass

        return ShortInterest(
            symbol=sym,
            short_interest=_safe_int(item.get("sharesShort")),
            short_float_pct=_parse_pct(item.get("shortFloat")),
            days_to_cover=_safe_float(item.get("shortRatio")),
            settlement_date=settlement_date,
            source=_SOURCE,
            fetched_at=now,
        )

    def get_peers(self, symbol: str) -> list[PeerCompany]:
        """Fetch peer companies via FMP ``/stable/stock-peers``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("stock-peers", params={"symbol": sym})

        if not data or not isinstance(data, list):
            return []

        results: list[PeerCompany] = []
        for item in data:
            try:
                peer_sym = (item.get("symbol") or "").strip().upper()
                if not peer_sym:
                    continue
                results.append(
                    PeerCompany(
                        symbol=peer_sym,
                        name=item.get("companyName") or None,
                        price=_safe_float(item.get("price")),
                        market_cap=_safe_int(item.get("mktCap")),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping FMP peer entry %s: %s", item, exc)
                continue

        return results

    def get_market_sentiment(self) -> MarketSentiment:
        """Fetch market-wide put/call ratio via FMP ``/v3/put_call_ratio``."""
        now = utc_now()

        data = self._get("put_call_ratio")

        if not data or not isinstance(data, list) or len(data) == 0:
            raise ProviderError(
                code="DATA_NOT_FOUND",
                message="No market sentiment data available",
                provider=_SOURCE,
                retry_safe=False,
            )

        item = data[0]

        as_of_date = None
        date_str = item.get("date")
        if date_str:
            try:
                as_of_date = parse_iso_date(date_str)
            except (ValueError, TypeError):
                pass

        return MarketSentiment(
            pcr_equity=_safe_float(item.get("putCallRatioEquity")),
            pcr_index=_safe_float(item.get("putCallRatioIndex")),
            pcr_total=_safe_float(item.get("putCallRatio")),
            as_of_date=as_of_date,
            source=_SOURCE,
            fetched_at=now,
        )
