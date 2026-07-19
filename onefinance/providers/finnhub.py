"""Finnhub provider adapter.

Uses the Finnhub REST API (https://finnhub.io/api/v1).
Requires an API key set via the ``FINNHUB_API_KEY`` environment variable.

Free tier: 60 calls/minute. Realtime quotes available on free tier
(some data may have 20-min delay).

See design doc §6, §7, §9 for the provider contract.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar

import httpx

from onefinance.core.errors import ConfigError, NotSupportedError, ProviderError
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    EarningsCalendarEntry,
    EarningsRecord,
    EconomicEvent,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    NewsArticle,
    PeerCompany,
    PriceBar,
    Quote,
)
from onefinance.providers._http import HttpProviderMixin
from onefinance.providers._utils import (
    _safe_float,
    _safe_int,
    change_pct_from_prev_close,
    format_period,
    normalize_symbol,
    parse_iso_date,
    utc_now,
)
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "finnhub"
_BASE_URL = "https://finnhub.io/api/v1"


def _xbrl_float(vals: dict[str, Any], concepts: list[str]) -> float:
    """Extract first matching XBRL concept value as float; default 0.0."""
    for concept in concepts:
        v = vals.get(concept)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                continue
    return 0.0


def _xbrl_float_opt(vals: dict[str, Any], concepts: list[str]) -> float | None:
    """Like _xbrl_float but returns None instead of 0.0 when no concept matches."""
    for concept in concepts:
        v = vals.get(concept)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                continue
    return None


class FinnhubProvider(HttpProviderMixin, BaseProvider):
    """Provider adapter for Finnhub.

    Parameters
    ----------
    api_key:
        Finnhub API key. If ``None``, reads from ``FINNHUB_API_KEY`` env var.
    timeout:
        HTTP request timeout in seconds.
    base_url:
        Override the base URL (useful for testing).
    http_client:
        Optional shared ``httpx.Client`` (useful for testing or pooling).
    """

    name = _SOURCE

    # Finnhub's /quote payload (c, d, dp, h, l, o, pc, t) carries no volume
    # field, so every quote needs a volume augment from another provider.
    # Declaring it lets the router start that filler call concurrently with
    # the primary request instead of serially after it.
    KNOWN_MISSING_FIELDS: ClassVar[dict[str, frozenset[str]]] = {
        "quote": frozenset({"volume"}),
    }

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 10,
        base_url: str = _BASE_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("FINNHUB_API_KEY")
        if not self._api_key:
            raise ConfigError(
                "FINNHUB_API_KEY not set. Set it in your environment or pass api_key="
            )
        self._base_url = base_url
        super().__init__(timeout=float(timeout), http_client=http_client)

    # -------------------------------------------------------------------
    # HTTP helper
    # -------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Authenticated GET to Finnhub API. Returns decoded JSON.

        Rate-limit detection (HTTP 429 + ``Retry-After``) is delegated to
        :class:`HttpProviderMixin`. The default ``_rate_limit_signals`` handles
        the Finnhub case.
        """
        url = f"{self._base_url}/{path}"
        req_params = dict(params or {})
        req_params["token"] = self._api_key

        resp = self._request("GET", url, params=req_params)

        if resp.status_code == 403:
            # 403 = plan restriction for this symbol/endpoint — treat as not_supported
            raise NotSupportedError(self.name, path, http_status=403)
        self._raise_for_status(resp)

        return resp.json()

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
        """Fetch daily OHLCV bars via ``/stock/candle``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        start_ts = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp())
        end_ts = int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC).timestamp())

        resolution_map = {
            "1m": "1",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "1h": "60",
            "60m": "60",
            "1d": "D",
            "1wk": "W",
            "1mo": "M",
        }
        res = resolution_map.get(interval, "D")

        data = self._get(
            "stock/candle",
            params={
                "symbol": sym,
                "resolution": res,
                "from": start_ts,
                "to": end_ts,
            },
        )

        if not data or data.get("s") == "no_data":
            return []

        closes = data.get("c", [])
        highs = data.get("h", [])
        lows = data.get("l", [])
        opens = data.get("o", [])
        timestamps = data.get("t", [])
        volumes = data.get("v", [])

        bars: list[PriceBar] = []
        for i in range(len(closes)):
            try:
                bar_ts = datetime.fromtimestamp(timestamps[i], tz=UTC)
                bar_date = bar_ts.date()
                bars.append(
                    PriceBar(
                        symbol=sym,
                        date=bar_date,
                        timestamp=bar_ts,
                        open=float(opens[i]),
                        high=float(highs[i]),
                        low=float(lows[i]),
                        close=float(closes[i]),
                        adj_close=float(closes[i]),
                        volume=int(volumes[i]),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping Finnhub bar for %s: %s", sym, exc)
                continue

        return bars

    # -------------------------------------------------------------------
    # get_quote — Type B
    # -------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        """Fetch current quote via ``/quote``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("quote", params={"symbol": sym})

        if not data or data.get("c") is None:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No quote found for '{symbol}' via Finnhub",
                provider=self.name,
                retry_safe=False,
            )

        ts = data.get("t")
        timestamp = datetime.fromtimestamp(ts, tz=UTC) if ts else now
        price = float(data["c"])
        prev_close = _safe_float(data.get("pc"))

        return Quote(
            symbol=sym,
            timestamp=timestamp,
            price=price,
            bid=None,
            ask=None,
            volume=int(data.get("v", 0) or 0),
            prev_close=prev_close,
            change_pct=change_pct_from_prev_close(price, prev_close),
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # get_info — Type A
    # -------------------------------------------------------------------

    def get_info(self, symbol: str) -> CompanyInfo:
        """Fetch company profile via ``/stock/profile2``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("stock/profile2", params={"symbol": sym})

        if not data or not data.get("name"):
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No profile found for '{symbol}' via Finnhub",
                provider=self.name,
                retry_safe=False,
            )

        raw_currency = data.get("currency")
        currency: str | None = None
        if raw_currency and isinstance(raw_currency, str) and len(raw_currency) == 3:
            currency = raw_currency.upper()

        market_cap_m = data.get("marketCapitalization")
        market_cap = float(market_cap_m) * 1_000_000 if market_cap_m is not None else None

        return CompanyInfo(
            symbol=sym,
            name=data.get("name") or symbol,
            exchange=data.get("exchange"),
            sector=None,
            industry=data.get("finnhubIndustry"),
            country=data.get("country"),
            market_cap=market_cap,
            beta=None,  # Not in profile2
            shares_outstanding=_safe_int(data.get("shareOutstanding")),
            description=None,
            website=data.get("weburl"),
            employees=None,
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
        """Fetch as-reported XBRL financials via ``/financials-reported``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        stmt_map = {"income": "ic", "balance": "bs", "cashflow": "cf"}
        stmt_code = stmt_map.get(statement)
        if stmt_code is None:
            raise ProviderError(
                code="INVALID_ARGUMENT",
                message=(
                    f"Unknown statement type: '{statement}'. "
                    "Use 'income', 'balance', or 'cashflow'."
                ),
                provider=self.name,
                retry_safe=False,
            )

        freq = "quarterly" if period == "quarterly" else "annual"
        data = self._get(
            "financials-reported",
            params={
                "symbol": sym,
                "statement": stmt_code,
                "freq": freq,
            },
        )

        entries = data.get("data", []) if isinstance(data, dict) else []
        if not entries:
            return []

        results: list[IncomeStatement | BalanceSheet | CashFlow] = []
        for entry in entries[:5]:
            report = entry.get("report", {})
            concepts = report.get(stmt_code, [])
            vals: dict[str, Any] = {c.get("concept", ""): c.get("value") for c in concepts}

            end_date_str = entry.get("endDate", "")
            try:
                fiscal_date = parse_iso_date(end_date_str)
            except (ValueError, TypeError):
                continue

            year = entry.get("year", fiscal_date.year)
            q = entry.get("quarter", 0)
            period_str = format_period(year, q) if q else format_period(year, "FY")

            try:
                if statement == "income":
                    results.append(
                        IncomeStatement(
                            symbol=sym,
                            period=period_str,
                            fiscal_date=fiscal_date,
                            revenue=_xbrl_float(
                                vals,
                                [
                                    "us-gaap:Revenues",
                                    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                                ],
                            ),
                            cost_of_revenue=_xbrl_float(
                                vals,
                                [
                                    "us-gaap:CostOfRevenue",
                                    "us-gaap:CostOfGoodsAndServicesSold",
                                ],
                            ),
                            gross_profit=_xbrl_float(vals, ["us-gaap:GrossProfit"]),
                            operating_income=_xbrl_float(vals, ["us-gaap:OperatingIncomeLoss"]),
                            net_income=_xbrl_float(vals, ["us-gaap:NetIncomeLoss"]),
                            eps_basic=_xbrl_float(vals, ["us-gaap:EarningsPerShareBasic"]),
                            eps_diluted=_xbrl_float(vals, ["us-gaap:EarningsPerShareDiluted"]),
                            currency="USD",
                            source=_SOURCE,
                            fetched_at=now,
                            research_and_development=_xbrl_float_opt(
                                vals,
                                [
                                    "us-gaap:ResearchAndDevelopmentExpense",
                                ],
                            ),
                            sga_expenses=_xbrl_float_opt(
                                vals,
                                [
                                    "us-gaap:SellingGeneralAndAdministrativeExpense",
                                ],
                            ),
                        )
                    )
                elif statement == "balance":
                    results.append(
                        BalanceSheet(
                            symbol=sym,
                            period=period_str,
                            fiscal_date=fiscal_date,
                            total_assets=_xbrl_float(vals, ["us-gaap:Assets"]),
                            total_liabilities=_xbrl_float(vals, ["us-gaap:Liabilities"]),
                            total_equity=_xbrl_float(
                                vals,
                                [
                                    "us-gaap:StockholdersEquity",
                                    "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                                ],
                            ),
                            cash_and_equivalents=_xbrl_float(
                                vals,
                                [
                                    "us-gaap:CashAndCashEquivalentsAtCarryingValue",
                                ],
                            ),
                            total_debt=_xbrl_float(
                                vals,
                                [
                                    "us-gaap:LongTermDebt",
                                    "us-gaap:LongTermDebtNoncurrent",
                                ],
                            ),
                            currency="USD",
                            source=_SOURCE,
                            fetched_at=now,
                            total_current_assets=_xbrl_float_opt(
                                vals,
                                [
                                    "us-gaap:AssetsCurrent",
                                ],
                            ),
                            total_current_liabilities=_xbrl_float_opt(
                                vals,
                                [
                                    "us-gaap:LiabilitiesCurrent",
                                ],
                            ),
                            inventory=_xbrl_float_opt(
                                vals,
                                [
                                    "us-gaap:InventoryNet",
                                ],
                            ),
                            goodwill=_xbrl_float_opt(
                                vals,
                                [
                                    "us-gaap:Goodwill",
                                ],
                            ),
                        )
                    )
                else:  # cashflow
                    op_cf = _xbrl_float(
                        vals,
                        [
                            "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                        ],
                    )
                    capex = _xbrl_float(
                        vals,
                        [
                            "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                        ],
                    )
                    results.append(
                        CashFlow(
                            symbol=sym,
                            period=period_str,
                            fiscal_date=fiscal_date,
                            operating_cash_flow=op_cf,
                            capital_expenditure=capex,
                            free_cash_flow=op_cf - capex,
                            dividends_paid=_xbrl_float(vals, ["us-gaap:PaymentsOfDividends"]),
                            currency="USD",
                            source=_SOURCE,
                            fetched_at=now,
                            depreciation_and_amortization=_xbrl_float_opt(
                                vals,
                                [
                                    "us-gaap:DepreciationDepletionAndAmortization",
                                    "us-gaap:DepreciationAndAmortization",
                                ],
                            ),
                            stock_based_compensation=_xbrl_float_opt(
                                vals,
                                [
                                    "us-gaap:ShareBasedCompensation",
                                ],
                            ),
                        )
                    )
            except Exception as exc:
                logger.warning("Skipping Finnhub financial entry for %s: %s", sym, exc)
                continue

        return results

    # -------------------------------------------------------------------
    # get_ratios — Type C
    # -------------------------------------------------------------------

    def get_ratios(self, symbol: str, period: str) -> list[FinancialRatios]:
        """Fetch current financial metrics via ``/stock/metric``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("stock/metric", params={"symbol": sym, "metric": "all"})
        metric = data.get("metric", {}) if isinstance(data, dict) else {}
        if not metric:
            return []

        return [
            FinancialRatios(
                symbol=sym,
                period="current",
                fiscal_date=date.today(),
                pe_ratio=_safe_float(metric.get("peAnnual") or metric.get("peTTM")),
                pb_ratio=_safe_float(metric.get("pbAnnual") or metric.get("pbQuarterly")),
                ps_ratio=_safe_float(metric.get("psAnnual") or metric.get("psTTM")),
                debt_to_equity=_safe_float(metric.get("totalDebt/totalEquityAnnual")),
                current_ratio=_safe_float(metric.get("currentRatioAnnual")),
                return_on_equity=_safe_float(metric.get("roeTTM")),
                return_on_assets=_safe_float(metric.get("roaRfy")),
                gross_margin=_safe_float(
                    metric.get("grossMarginAnnual") or metric.get("grossMarginTTM")
                ),
                operating_margin=_safe_float(
                    metric.get("operatingMarginAnnual") or metric.get("operatingMarginTTM")
                ),
                net_margin=_safe_float(
                    metric.get("netProfitMarginAnnual") or metric.get("netProfitMarginTTM")
                ),
                dividend_yield=_safe_float(metric.get("dividendYieldIndicatedAnnual")),
                quick_ratio=_safe_float(metric.get("quickRatioAnnual")),
                enterprise_value=_safe_float(metric.get("enterpriseValue")),
                roic=_safe_float(metric.get("roicTTM")),
                book_value_per_share=_safe_float(metric.get("bookValuePerShareAnnual")),
                revenue_per_share=_safe_float(
                    metric.get("revenuePerShareAnnual") or metric.get("revenuePerShareTTM")
                ),
                free_cash_flow_yield=_safe_float(metric.get("fcfYieldTTM")),
                source=_SOURCE,
                fetched_at=now,
            )
        ]

    # -------------------------------------------------------------------
    # get_earnings — Type C
    # -------------------------------------------------------------------

    def get_earnings(self, symbol: str) -> list[EarningsRecord]:
        """Fetch earnings surprises via ``/stock/earnings``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("stock/earnings", params={"symbol": sym, "limit": 8})

        if not data or not isinstance(data, list):
            return []

        results: list[EarningsRecord] = []
        for item in data:
            period_str = item.get("period")
            if not period_str:
                continue
            try:
                fiscal_date = parse_iso_date(period_str)
            except (ValueError, TypeError):
                continue

            year = item.get("year", fiscal_date.year)
            q = item.get("quarter", 0)
            period_label = format_period(year, q) if q else format_period(year, "FY")

            results.append(
                EarningsRecord(
                    symbol=sym,
                    period=period_label,
                    fiscal_date=fiscal_date,
                    eps_actual=_safe_float(item.get("actual")),
                    eps_estimate=_safe_float(item.get("estimate")),
                    eps_surprise=_safe_float(item.get("surprise")),
                    revenue_actual=None,
                    revenue_estimate=None,
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
        """Fetch insider transactions via ``/stock/insider-transactions``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("stock/insider-transactions", params={"symbol": sym})

        entries = data.get("data", []) if isinstance(data, dict) else []
        if not entries:
            return []

        results: list[InsiderTrade] = []
        for item in entries:
            filing_date_str = item.get("filingDate")
            if not filing_date_str:
                continue
            try:
                filing_d = parse_iso_date(filing_date_str[:10])
            except (ValueError, TypeError):
                continue

            if since and filing_d < since:
                continue

            trade_date_str = item.get("transactionDate")
            trade_d: date | None = None
            if trade_date_str:
                try:
                    trade_d = parse_iso_date(trade_date_str[:10])
                except (ValueError, TypeError):
                    pass

            code = (item.get("transactionCode") or "").upper()
            if code == "P":
                trade_type = "buy"
            elif code in ("S", "F"):
                trade_type = "sell"
            elif code in ("M", "A"):
                trade_type = "exercise"
            else:
                trade_type = code.lower() or "unknown"

            shares = abs(float(item.get("change", 0) or 0))
            price = _safe_float(item.get("transactionPrice"))
            total_value = (shares * price) if price is not None else None

            results.append(
                InsiderTrade(
                    symbol=sym,
                    filing_date=filing_d,
                    trade_date=trade_d,
                    insider_name=item.get("name", "Unknown"),
                    insider_title=item.get("source"),
                    trade_type=trade_type,
                    shares=shares,
                    price_per_share=price,
                    total_value=total_value,
                    shares_owned_after=_safe_float(item.get("share")),
                    source=_SOURCE,
                    fetched_at=now,
                )
            )

        return results

    # -------------------------------------------------------------------
    # get_corporate_actions — Type A
    # -------------------------------------------------------------------

    def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Fetch dividend and split history via Finnhub.

        Uses ``/stock/dividend`` and ``/stock/split`` with a 5-year lookback.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)
        start = (date.today() - timedelta(days=5 * 365)).isoformat()
        end = date.today().isoformat()

        actions: list[CorporateAction] = []

        try:
            div_data = self._get("stock/dividend", params={"symbol": sym, "from": start, "to": end})
            for item in div_data or []:
                try:
                    actions.append(
                        CorporateAction(
                            symbol=sym,
                            date=parse_iso_date(item["date"]),
                            action_type="dividend",
                            amount=_safe_float(item.get("amount")),
                            source=_SOURCE,
                            fetched_at=now,
                        )
                    )
                except Exception as exc:
                    logger.warning("Skipping Finnhub dividend for %s: %s", sym, exc)
        except Exception as exc:
            logger.warning("Finnhub dividend fetch failed for %s: %s", sym, exc)

        try:
            split_data = self._get("stock/split", params={"symbol": sym, "from": start, "to": end})
            for item in split_data or []:
                try:
                    from_factor = _safe_float(item.get("fromFactor")) or 1.0
                    to_factor = _safe_float(item.get("toFactor")) or 1.0
                    actions.append(
                        CorporateAction(
                            symbol=sym,
                            date=parse_iso_date(item["date"]),
                            action_type="split",
                            split_ratio=to_factor / from_factor,
                            source=_SOURCE,
                            fetched_at=now,
                        )
                    )
                except Exception as exc:
                    logger.warning("Skipping Finnhub split for %s: %s", sym, exc)
        except Exception as exc:
            logger.warning("Finnhub split fetch failed for %s: %s", sym, exc)

        return sorted(actions, key=lambda a: a.date, reverse=True)

    # -------------------------------------------------------------------
    # Alternative Data
    # -------------------------------------------------------------------

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        """Fetch recent news articles from Finnhub."""
        now = utc_now()
        sym = normalize_symbol(symbol)
        to_date = now.strftime("%Y-%m-%d")
        from_date = f"{now.year}-{max(1, now.month - 1):02d}-{now.day:02d}"

        data = self._get("/company-news", params={"symbol": sym, "from": from_date, "to": to_date})
        if not data or not isinstance(data, list):
            return []

        articles = []
        for n in data[:limit]:
            try:
                published_at = datetime.fromtimestamp(n.get("datetime", 0), UTC)
                articles.append(
                    NewsArticle(
                        symbol=sym,
                        title=n.get("headline", ""),
                        publisher=n.get("source", ""),
                        link=n.get("url", ""),
                        published_at=published_at,
                        summary=n.get("summary", ""),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Failed to parse news for %s: %s", sym, exc)
                continue
        return articles

    def get_analyst_data(self, symbol: str) -> AnalystData:
        """Fetch analyst price targets and ratings from Finnhub."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        pt_data = self._get("/stock/price-target", params={"symbol": sym})
        pt = pt_data if isinstance(pt_data, dict) else {}

        rec_data = self._get("/stock/recommendation", params={"symbol": sym})
        rt = rec_data[0] if isinstance(rec_data, list) and rec_data else {}

        if not pt and not rt:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No analyst data found for symbol '{symbol}' via Finnhub",
                provider=self.name,
                retry_safe=False,
            )

        return AnalystData(
            symbol=sym,
            target_high=_safe_float(pt.get("targetHigh")),
            target_low=_safe_float(pt.get("targetLow")),
            target_mean=_safe_float(pt.get("targetMean")),
            target_median=_safe_float(pt.get("targetMedian")),
            rating_buy=_safe_int(rt.get("buy")),
            rating_hold=_safe_int(rt.get("hold")),
            rating_sell=_safe_int(rt.get("sell")),
            rating_strong_buy=_safe_int(rt.get("strongBuy")),
            rating_strong_sell=_safe_int(rt.get("strongSell")),
            source=_SOURCE,
            fetched_at=now,
        )

    def get_forward_estimates(self, symbol: str) -> list[ForwardEstimates]:
        """Fetch analyst estimates from Finnhub."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        rev_data = self._get("stock/revenue-estimate", params={"symbol": sym})
        eps_data = self._get("stock/eps-estimate", params={"symbol": sym})

        raw: dict[str, dict[str, Any]] = {}

        if rev_data and "data" in rev_data:
            for item in rev_data["data"]:
                period = item.get("period")
                if not period:
                    continue
                raw.setdefault(period, {})["revenue_estimate"] = _safe_float(item.get("revenueAvg"))

        if eps_data and "data" in eps_data:
            for item in eps_data["data"]:
                period = item.get("period")
                if not period:
                    continue
                raw.setdefault(period, {})["eps_estimate"] = _safe_float(item.get("epsAvg"))

        return [
            ForwardEstimates(
                symbol=sym,
                period=period,
                revenue_estimate=fields.get("revenue_estimate"),
                eps_estimate=fields.get("eps_estimate"),
                source=_SOURCE,
                fetched_at=now,
            )
            for period, fields in raw.items()
        ]

    # -------------------------------------------------------------------
    # get_earnings_calendar — Type A
    # -------------------------------------------------------------------

    def get_earnings_calendar(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> list[EarningsCalendarEntry]:
        """Fetch earnings calendar via ``/calendar/earnings``.

        Supports ``from``/``to`` date filtering.  Without date params,
        Finnhub returns up to 1500 entries centred around today.
        """
        now = utc_now()
        params: dict[str, Any] = {}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()

        data = self._get("calendar/earnings", params=params)
        entries = data.get("earningsCalendar", []) if isinstance(data, dict) else []

        results: list[EarningsCalendarEntry] = []
        for item in entries:
            try:
                sym = (item.get("symbol") or "").strip().upper()
                date_str = item.get("date")
                if not sym or not date_str:
                    continue
                report_d = parse_iso_date(date_str)
                hour_raw = (item.get("hour") or "").lower().strip()
                time_of_day = hour_raw if hour_raw in ("bmo", "amc", "dmh") else None
                results.append(
                    EarningsCalendarEntry(
                        symbol=sym,
                        report_date=report_d,
                        year=item.get("year"),
                        quarter=item.get("quarter"),
                        eps_estimate=_safe_float(item.get("epsEstimate")),
                        eps_actual=_safe_float(item.get("epsActual")),
                        revenue_estimate=_safe_float(item.get("revenueEstimate")),
                        revenue_actual=_safe_float(item.get("revenueActual")),
                        time_of_day=time_of_day,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping Finnhub earnings calendar entry: %s", exc)
                continue

        return results

    def get_economic_calendar(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> list[EconomicEvent]:
        """Fetch macro economic events via Finnhub ``/calendar/economic``.

        ``impact`` integers: 0 = low, 1 = medium, 2 = high.
        """
        now = utc_now()
        params: dict[str, Any] = {}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()

        data = self._get("calendar/economic", params=params)
        entries = data.get("economicCalendar", []) if isinstance(data, dict) else []

        _IMPACT_MAP = {0: "low", 1: "medium", 2: "high"}

        results: list[EconomicEvent] = []
        for item in entries:
            try:
                event_name = (item.get("event") or "").strip()
                date_str = item.get("time") or item.get("date")
                if not event_name or not date_str:
                    continue
                date_part = date_str.split(" ")[0] if " " in date_str else date_str
                time_part = date_str.split(" ")[1][:5] if " " in date_str else None
                impact_raw = item.get("impact")
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
                        previous=_safe_float(item.get("prev")),
                        impact=_IMPACT_MAP.get(impact_raw) if impact_raw is not None else None,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping Finnhub economic calendar entry: %s", exc)
                continue

        return results

    def get_peers(self, symbol: str) -> list[PeerCompany]:
        """Fetch peer companies via Finnhub ``/stock/peers``.

        Returns symbol strings only — Finnhub does not supply name/price/mktCap.
        The queried symbol appears first in the response and is skipped.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("stock/peers", params={"symbol": sym})

        if not data or not isinstance(data, list):
            return []

        results: list[PeerCompany] = []
        for peer_sym_raw in data:
            try:
                peer_sym = (str(peer_sym_raw) or "").strip().upper()
                if not peer_sym or peer_sym == sym:
                    continue
                results.append(
                    PeerCompany(
                        symbol=peer_sym,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping Finnhub peer %s: %s", peer_sym_raw, exc)
                continue

        return results

    # -------------------------------------------------------------------
    # Rate-limit detection
    # -------------------------------------------------------------------

    def is_rate_limited(self, response: Any) -> bool:
        if isinstance(response, httpx.Response):
            return response.status_code == 429
        if isinstance(response, Exception):
            return "429" in str(response)
        return False

    def cooldown_for(self, response: Any) -> float:
        if isinstance(response, httpx.Response):
            try:
                return float(response.headers.get("Retry-After", "60"))
            except (ValueError, TypeError):
                pass
        return 60.0
