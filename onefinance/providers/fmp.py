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
    ProviderError,
    RateLimitError,
)
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    DCFValuation,
    EarningsRecord,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    NewsArticle,
    PriceBar,
    Quote,
    ScreenerResult,
)
from onefinance.providers._utils import _safe_float, _safe_int
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "fmp"
_BASE_URL = "https://financialmodelingprep.com/stable"


class FMPProvider(BaseProvider):
    """Provider adapter for Financial Modeling Prep (stable API).

    Parameters
    ----------
    api_key:
        FMP API key. If ``None``, reads from ``FMP_API_KEY`` env var.
    timeout:
        HTTP request timeout in seconds.
    base_url:
        Override the base URL (useful for testing).
    """

    name = _SOURCE

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 10,
        base_url: str = _BASE_URL,
    ) -> None:
        self._api_key = api_key or os.environ.get("FMP_API_KEY")
        if not self._api_key:
            raise ConfigError("FMP_API_KEY not set. Set it in your environment or pass api_key=")
        self._timeout = timeout
        self._base_url = base_url
        self._client = httpx.Client(timeout=timeout)

    # -------------------------------------------------------------------
    # HTTP helper
    # -------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Make an authenticated GET request to FMP stable API.

        Raises ``RateLimitError`` on 429 or "Limit Reach" responses,
        ``ProviderError`` on other failures.
        """
        url = f"{self._base_url}/{path}"
        req_params = dict(params or {})
        req_params["apikey"] = self._api_key

        try:
            resp = self._client.get(url, params=req_params)
        except httpx.HTTPError as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"FMP request failed: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        # Rate-limit detection
        if resp.status_code == 429:
            raise RateLimitError(
                provider=self.name,
                message="FMP rate limit hit (HTTP 429)",
                retry_after_seconds=3600,
            )

        if resp.status_code != 200:
            body = resp.text
            if "Limit Reach" in body or "limit reach" in body.lower():
                raise RateLimitError(
                    provider=self.name,
                    message=f"FMP daily quota exhausted: {body[:200]}",
                    retry_after_seconds=3600,
                )
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"FMP HTTP {resp.status_code}: {body[:200]}",
                provider=self.name,
                retry_safe=resp.status_code >= 500,
            )

        data = resp.json()

        # FMP sometimes returns error objects instead of arrays
        if isinstance(data, dict):
            error_msg = data.get("Error Message") or data.get("error")
            if error_msg:
                if "limit" in str(error_msg).lower():
                    raise RateLimitError(
                        provider=self.name,
                        message=f"FMP error: {error_msg}",
                        retry_after_seconds=3600,
                    )
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

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[PriceBar]:
        """Fetch OHLCV bars via FMP."""
        now = datetime.now(UTC)

        if interval in ("1d", "1wk", "1mo"):
            data = self._get(
                "historical-price-eod/full",
                params={
                    "symbol": symbol.upper(),
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                },
            )
        else:
            res_map = {
                "1m": "1min",
                "5m": "5min",
                "15m": "15min",
                "30m": "30min",
                "60m": "1hour",
                "1h": "1hour",
            }
            res = res_map.get(interval, "1hour")
            data = self._get(
                f"historical-chart/{res}/{symbol.upper()}",
                params={
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                },
            )

        if not data or not isinstance(data, list):
            return []

        bars: list[PriceBar] = []
        for item in data:
            try:
                dt_str = item["date"]
                if len(dt_str) > 10:
                    bar_ts = datetime.fromisoformat(dt_str).replace(tzinfo=UTC)
                    bar_date = bar_ts.date()
                else:
                    bar_date = date.fromisoformat(dt_str)
                    bar_ts = None

                bars.append(
                    PriceBar(
                        symbol=symbol.upper(),
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
                )
            except Exception as exc:
                logger.warning("Skipping FMP bar for %s: %s", symbol, exc)
                continue

        # FMP returns newest-first; reverse to chronological order
        bars.reverse()
        return bars

    # -------------------------------------------------------------------
    # get_quote — Type B
    # -------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        """Fetch realtime quote via ``/stable/quote``."""
        now = datetime.now(UTC)

        data = self._get("quote", params={"symbol": symbol.upper()})

        if not data:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No quote found for '{symbol}' via FMP",
                provider=self.name,
                retry_safe=False,
            )

        item = data[0] if isinstance(data, list) else data

        return Quote(
            symbol=symbol.upper(),
            timestamp=datetime.fromtimestamp(item["timestamp"], tz=UTC)
            if item.get("timestamp")
            else now,
            price=float(item["price"]),
            bid=None,  # FMP quote doesn't include bid/ask
            ask=None,
            volume=int(item.get("volume", 0)),
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # get_info — Type A
    # -------------------------------------------------------------------

    def get_info(self, symbol: str) -> CompanyInfo:
        """Fetch company profile via ``/stable/profile``."""
        now = datetime.now(UTC)

        data = self._get("profile", params={"symbol": symbol.upper()})

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
            symbol=symbol.upper(),
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
        now = datetime.now(UTC)

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
            params={"symbol": symbol.upper(), "period": fmp_period, "limit": 5},
        )

        if not data or not isinstance(data, list):
            return []

        if statement == "income":
            return [self._normalise_income(item, symbol, now) for item in data]
        elif statement == "balance":
            return [self._normalise_balance(item, symbol, now) for item in data]
        else:  # cashflow
            return [self._normalise_cashflow(item, symbol, now) for item in data]

    def _normalise_income(
        self, item: dict[str, Any], symbol: str, now: datetime
    ) -> IncomeStatement:
        fiscal_year = item.get("fiscalYear", "")
        fmp_period = item.get("period", "FY")
        period_str = f"{fiscal_year}-{fmp_period}"

        return IncomeStatement(
            symbol=symbol.upper(),
            period=period_str,
            fiscal_date=date.fromisoformat(item["date"]),
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
        period_str = f"{fiscal_year}-{fmp_period}"

        return BalanceSheet(
            symbol=symbol.upper(),
            period=period_str,
            fiscal_date=date.fromisoformat(item["date"]),
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
        period_str = f"{fiscal_year}-{fmp_period}"

        return CashFlow(
            symbol=symbol.upper(),
            period=period_str,
            fiscal_date=date.fromisoformat(item["date"]),
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
        now = datetime.now(UTC)

        fmp_period = "quarter" if period == "quarterly" else "annual"

        data = self._get(
            "ratios",
            params={"symbol": symbol.upper(), "period": fmp_period, "limit": 5},
        )

        if not data or not isinstance(data, list):
            return []

        results: list[FinancialRatios] = []
        for item in data:
            fiscal_year = item.get("fiscalYear", "")
            fmp_period_label = item.get("period", "FY")
            period_str = f"{fiscal_year}-{fmp_period_label}"

            results.append(
                FinancialRatios(
                    symbol=symbol.upper(),
                    period=period_str,
                    fiscal_date=date.fromisoformat(item["date"]),
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
        now = datetime.now(UTC)

        data = self._get("earnings", params={"symbol": symbol.upper()})

        if not data or not isinstance(data, list):
            return []

        results: list[EarningsRecord] = []
        for item in data:
            item_date = item.get("date")
            if not item_date:
                continue

            # Derive period from date
            d = date.fromisoformat(item_date)
            quarter = (d.month - 1) // 3 + 1
            period_str = f"{d.year}-Q{quarter}"

            results.append(
                EarningsRecord(
                    symbol=symbol.upper(),
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
        now = datetime.now(UTC)

        params: dict[str, Any] = {"symbol": symbol.upper(), "limit": 100}

        data = self._get("insider-trading", params=params)

        if not data or not isinstance(data, list):
            return []

        results: list[InsiderTrade] = []
        for item in data:
            filing_date_str = item.get("filingDate")
            if not filing_date_str:
                continue

            filing_d = date.fromisoformat(filing_date_str[:10])

            # Filter by since date
            if since and filing_d < since:
                continue

            trade_date_str = item.get("transactionDate")
            trade_d = date.fromisoformat(trade_date_str[:10]) if trade_date_str else None

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
                    symbol=symbol.upper(),
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
        now = datetime.now(UTC)

        data = self._get("discounted-cash-flow", params={"symbol": symbol.upper()})

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
            symbol=symbol.upper(),
            dcf=float(dcf_value),
            stock_price=float(stock_price),
            dcf_date=date.fromisoformat(dcf_date_str) if dcf_date_str else date.today(),
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # Alternative Data
    # -------------------------------------------------------------------

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        """Fetch recent news articles from FMP."""
        from onefinance.core.models import NewsArticle

        now = datetime.now(UTC)
        url = f"{self._base_url}/stock_news"
        data = self._get(url, params={"tickers": symbol, "limit": limit})
        if not data or not isinstance(data, list):
            return []

        articles = []
        for n in data:
            try:
                published_str = n.get("publishedDate")
                published_at = datetime.fromisoformat(published_str) if published_str else now
                articles.append(
                    NewsArticle(
                        symbol=symbol.upper(),
                        title=n.get("title", ""),
                        publisher=n.get("site", ""),
                        link=n.get("url", ""),
                        published_at=published_at,
                        summary=n.get("text"),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Failed to parse news for %s: %s", symbol, exc)
                continue
        return articles

    def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Fetch dividend and split history from FMP."""
        from onefinance.core.models import CorporateAction

        now = datetime.now(UTC)
        actions = []

        # Dividends
        div_url = f"{self._base_url}/historical-price-full/stock_dividend/{symbol}"
        div_data = self._get(div_url)
        if isinstance(div_data, dict) and "historical" in div_data:
            for d in div_data["historical"]:
                try:
                    actions.append(
                        CorporateAction(
                            symbol=symbol.upper(),
                            date=date.fromisoformat(d["date"]),
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

        # Splits
        split_url = f"{self._base_url}/historical-price-full/stock_split/{symbol}"
        split_data = self._get(split_url)
        if isinstance(split_data, dict) and "historical" in split_data:
            for s in split_data["historical"]:
                try:
                    num = float(s.get("numerator", 0))
                    den = float(s.get("denominator", 1))
                    ratio = num / den if den != 0 else 0
                    actions.append(
                        CorporateAction(
                            symbol=symbol.upper(),
                            date=date.fromisoformat(s["date"]),
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

        now = datetime.now(UTC)
        url = f"{self._base_url}/institutional-holder/{symbol}"
        data = self._get(url)
        if not data or not isinstance(data, list):
            return []

        holders = []
        for h in data:
            try:
                holders.append(
                    InstitutionalHolder(
                        symbol=symbol.upper(),
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
                logger.warning("Failed to parse institutional holder for %s: %s", symbol, exc)
                continue
        return holders

    def get_analyst_data(self, symbol: str) -> AnalystData:
        """Fetch analyst price targets and ratings from FMP."""
        from onefinance.core.models import AnalystData

        now = datetime.now(UTC)

        # Price Targets
        pt_url = f"{self._base_url}/price-target-consensus"
        pt_data = self._get(pt_url, params={"symbol": symbol})
        pt = pt_data[0] if isinstance(pt_data, list) and pt_data else {}

        # Ratings
        rating_url = f"{self._base_url}/analyst-ratings"
        rating_data = self._get(rating_url, params={"symbol": symbol})
        rt = rating_data[0] if isinstance(rating_data, list) and rating_data else {}

        if not pt and not rt:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No analyst data found for symbol '{symbol}' via FMP",
                provider=self.name,
                retry_safe=False,
            )

        def _sf(v: Any) -> float | None:
            return float(v) if v is not None else None

        def _si(v: Any) -> int | None:
            return int(v) if v is not None else None

        return AnalystData(
            symbol=symbol.upper(),
            target_high=_sf(pt.get("targetHigh")),
            target_low=_sf(pt.get("targetLow")),
            target_mean=_sf(pt.get("targetConsensus")),
            target_median=_sf(pt.get("targetMedian")),
            rating_buy=_si(rt.get("analystRatingsBuy")),
            rating_hold=_si(rt.get("analystRatingsHold")),
            rating_sell=_si(rt.get("analystRatingsSell")),
            rating_strong_buy=_si(rt.get("analystRatingsStrongBuy")),
            rating_strong_sell=_si(rt.get("analystRatingsStrongSell")),
            source=_SOURCE,
            fetched_at=now,
        )

    def screen_stocks(self, query: str) -> list[ScreenerResult]:
        """Screen stocks via FMP /stock-screener.

        *query* should be a URL-encoded string like
        ``'marketCapMoreThan=1000000000&sector=Technology'``.
        """
        now = datetime.now(UTC)
        # parse query string into dict
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

    def get_forward_estimates(self, symbol: str) -> list[ForwardEstimates]:
        """Fetch consensus analyst estimates via ``/stable/analyst-estimates``."""
        now = datetime.now(UTC)

        # FMP v3 endpoint: analyst-estimates/{symbol}
        data = self._get("analyst-estimates", params={"symbol": symbol.upper(), "limit": 10})

        if not data:
            return []

        results = []
        for item in data:
            # FMP returns historical and forward estimates
            # fiscalDate like "2025-12-31"
            f_date = None
            if item.get("date"):
                try:
                    f_date = date.fromisoformat(item["date"])
                except ValueError:
                    pass

            # Label period based on year
            period = f_date.strftime("%Y-FY") if f_date else "forward"

            results.append(
                ForwardEstimates(
                    symbol=symbol.upper(),
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
