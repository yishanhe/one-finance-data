"""SEC EDGAR provider adapter.

Uses the SEC's free XBRL data APIs (https://data.sec.gov) — no API key, but
the SEC requires a descriptive ``User-Agent`` identifying the caller (set via
the ``EDGAR_USER_AGENT`` env var, else a sensible default). Rate limit is
10 req/s per IP.

Supports: financials (income / balance / cashflow), sourced from the XBRL
``companyfacts`` endpoint — the authoritative primary filings, not a reseller.

Statements are reconstructed from raw XBRL facts:
  * Periods are keyed by each fact's own ``end`` date plus a duration filter,
    because a filing's ``fy``/``fp`` context is unreliable (a 10-Q's fact list
    includes prior-year comparatives tagged with the *filing's* fy/fp).
  * Annual = 10-K facts spanning ~a year; quarterly = 10-Q facts spanning
    ~a quarter (the discrete ~90-day fact, not the year-to-date cumulative one
    that shares the same fp).
  * Q4 is intentionally absent: discrete Q4 is never filed in a 10-Q (it lives
    in the 10-K as the full year). We do not synthesise it from FY − 9mo.
  * A period is emitted only if its anchor metric (revenue / total assets /
    operating cash flow) is present, so we never report a real company's
    revenue as 0.0.

Coverage is **US 10-K/10-Q filers only**. Symbols EDGAR structurally can't
serve — ETFs/non-filers (no CIK) and 20-F/40-F foreign filers / ADRs (CIK but
no 10-K/10-Q facts) — raise ``NotSupportedError``, so the router falls through
to the next provider and negative-caches the symbol, rather than benching
EDGAR in cooldown or caching an empty result over a provider that can serve it.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

import httpx

from onefinance.core.errors import NotSupportedError, ProviderError
from onefinance.core.models import (
    BalanceSheet,
    CashFlow,
    IncomeStatement,
)
from onefinance.providers._http import HttpProviderMixin
from onefinance.providers._utils import _safe_float, normalize_symbol, parse_iso_date, utc_now
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "edgar"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_DEFAULT_UA = "onefinance (https://pypi.org/project/onefinance; contact via project page)"

# XBRL us-gaap tag fallbacks per normalised field. First tag with data for a
# period wins. Anchor field (first key) gates whether a period is emitted.
_INCOME_TAGS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "cost_of_revenue": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_basic": ["EarningsPerShareBasic"],
    "eps_diluted": ["EarningsPerShareDiluted"],
}
_BALANCE_TAGS: dict[str, list[str]] = {
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "total_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "cash_and_equivalents": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "total_debt": [
        "LongTermDebt",
        "LongTermDebtNoncurrent",
        "DebtLongtermAndShorttermCombinedAmount",
    ],
}
_CASHFLOW_TAGS: dict[str, list[str]] = {
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capital_expenditure": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
    ],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
}

# Duration-fact span windows (days) for annual vs quarterly.
_ANNUAL_SPAN = (350, 380)
_QUARTER_SPAN = (80, 100)


class SecEdgarProvider(HttpProviderMixin, BaseProvider):
    """Provider adapter for SEC EDGAR XBRL data (keyless)."""

    name = _SOURCE

    def __init__(
        self,
        timeout: int = 15,
        http_client: httpx.Client | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._user_agent = user_agent or os.environ.get("EDGAR_USER_AGENT") or _DEFAULT_UA
        self._cik_cache: dict[str, int] | None = None
        super().__init__(timeout=float(timeout), http_client=http_client)

    # -------------------------------------------------------------------
    # Rate-limit interface
    # -------------------------------------------------------------------

    def is_rate_limited(self, response: Any) -> bool:
        if isinstance(response, httpx.Response):
            return response.status_code == 429
        if isinstance(response, Exception):
            return "429" in str(response)
        return False

    def cooldown_for(self, response: Any) -> float:
        return self._default_rate_limit_cooldown_s

    # -------------------------------------------------------------------
    # HTTP helper + CIK resolution
    # -------------------------------------------------------------------

    def _get_json(self, url: str) -> Any:
        """GET a SEC JSON resource with the required User-Agent header."""
        resp = self._request("GET", url, headers={"User-Agent": self._user_agent})
        if resp.status_code == 404:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"SEC EDGAR: resource not found ({url})",
                provider=self.name,
                retry_safe=False,
                http_status=404,
            )
        self._raise_for_status(resp)
        return resp.json()

    def _cik_for(self, symbol: str) -> int:
        """Resolve a ticker to its zero-paddable CIK integer."""
        if self._cik_cache is None:
            data = self._get_json(_TICKERS_URL)
            cache: dict[str, int] = {}
            # company_tickers.json is {"0": {"cik_str": int, "ticker": str, ...}, ...}
            for row in (data or {}).values():
                try:
                    cache[str(row["ticker"]).upper()] = int(row["cik_str"])
                except (KeyError, TypeError, ValueError):
                    continue
            self._cik_cache = cache

        cik = self._cik_cache.get(symbol.upper())
        if cik is None:
            # No CIK (ETF, non-filer, unknown ticker). EDGAR can't serve it —
            # NotSupportedError so the router falls through + negative-caches,
            # rather than ProviderError which would put EDGAR in cooldown and
            # bench it for *other* symbols it can serve.
            raise NotSupportedError(self.name, "financials")
        return cik

    # -------------------------------------------------------------------
    # get_financials
    # -------------------------------------------------------------------

    def get_financials(
        self,
        symbol: str,
        statement: str,
        period: str,
    ) -> list[IncomeStatement | BalanceSheet | CashFlow]:
        """Reconstruct income / balance / cashflow statements from XBRL companyfacts.

        ``statement``: ``"income"`` | ``"balance"`` | ``"cashflow"``.
        ``period``: ``"annual"`` | ``"quarterly"`` (quarterly omits Q4 — see module docstring).
        """
        if statement not in ("income", "balance", "cashflow"):
            raise ProviderError(
                code="INVALID_ARGUMENT",
                message=f"Unknown statement '{statement}'. Use 'income', 'balance', or 'cashflow'.",
                provider=self.name,
                retry_safe=False,
            )

        sym = normalize_symbol(symbol)
        cik = self._cik_for(sym)
        try:
            facts = self._get_json(_FACTS_URL.format(cik=cik))
        except ProviderError as exc:
            # No XBRL facts filed for this CIK (e.g. a registered entity that
            # never filed). Structurally unservable → fall through cleanly.
            if exc.http_status == 404:
                raise NotSupportedError(self.name, "financials", http_status=404) from exc
            raise
        gaap = (facts.get("facts") or {}).get("us-gaap") or {}
        annual = period != "quarterly"
        is_duration = statement != "balance"  # balance items are instantaneous

        tag_map = {
            "income": _INCOME_TAGS,
            "balance": _BALANCE_TAGS,
            "cashflow": _CASHFLOW_TAGS,
        }[statement]
        anchor = next(iter(tag_map))

        # field -> {end_date: chosen fact}
        field_facts: dict[str, dict[date, dict[str, Any]]] = {
            field: self._field_by_end(gaap, tags, annual=annual, is_duration=is_duration)
            for field, tags in tag_map.items()
        }

        now = utc_now()
        results: list[IncomeStatement | BalanceSheet | CashFlow] = []
        for end, anchor_fact in sorted(field_facts[anchor].items()):
            fp = "FY" if annual else str(anchor_fact.get("fp") or "Q")
            period_label = f"{end.year}-{fp}"

            def val(field: str) -> float:
                f = field_facts.get(field, {}).get(end)
                return _safe_float(f["val"]) or 0.0 if f else 0.0

            if statement == "income":
                results.append(
                    IncomeStatement(
                        symbol=sym,
                        period=period_label,
                        fiscal_date=end,
                        revenue=val("revenue"),
                        cost_of_revenue=val("cost_of_revenue"),
                        gross_profit=val("gross_profit"),
                        operating_income=val("operating_income"),
                        net_income=val("net_income"),
                        eps_basic=val("eps_basic"),
                        eps_diluted=val("eps_diluted"),
                        currency="USD",
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            elif statement == "balance":
                results.append(
                    BalanceSheet(
                        symbol=sym,
                        period=period_label,
                        fiscal_date=end,
                        total_assets=val("total_assets"),
                        total_liabilities=val("total_liabilities"),
                        total_equity=val("total_equity"),
                        cash_and_equivalents=val("cash_and_equivalents"),
                        total_debt=val("total_debt"),
                        currency="USD",
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            else:
                ocf = val("operating_cash_flow")
                capex = val("capital_expenditure")
                results.append(
                    CashFlow(
                        symbol=sym,
                        period=period_label,
                        fiscal_date=end,
                        operating_cash_flow=ocf,
                        capital_expenditure=capex,
                        free_cash_flow=ocf - capex,
                        dividends_paid=val("dividends_paid"),
                        currency="USD",
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )

        if not results:
            # A successful companyfacts fetch with no matching 10-K/10-Q facts
            # means EDGAR structurally can't serve this symbol — e.g. a 20-F/40-F
            # foreign filer (ADR) or non-filer. Signal NotSupportedError (not an
            # empty success) so the router falls through to the next provider and
            # negative-caches this symbol, rather than caching an empty result
            # over a provider that *can* serve it.
            raise NotSupportedError(self.name, "financials")

        results.sort(key=lambda s: s.fiscal_date, reverse=True)
        return results

    # -------------------------------------------------------------------
    # Internal: XBRL fact selection
    # -------------------------------------------------------------------

    def _field_by_end(
        self,
        gaap: dict[str, Any],
        tags: list[str],
        *,
        annual: bool,
        is_duration: bool,
    ) -> dict[date, dict[str, Any]]:
        """Map period-end date → the chosen XBRL fact for *field*, across fallback tags.

        Walks fallback tags in order; the first tag that yields a fact for a
        given end date wins. Among multiple facts for the same end (restatements,
        YTD vs discrete), keeps the discrete one matching the duration window and
        the most recently filed.
        """
        out: dict[date, dict[str, Any]] = {}
        for tag in tags:
            tag_data = gaap.get(tag)
            if not tag_data:
                continue
            units = tag_data.get("units") or {}
            unit_facts = units.get("USD") or units.get("USD/shares") or []
            for fact in unit_facts:
                if not self._period_match(fact, annual=annual, is_duration=is_duration):
                    continue
                try:
                    end = parse_iso_date(fact["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                prev = out.get(end)
                if prev is None or str(fact.get("filed", "")) > str(prev.get("filed", "")):
                    out[end] = fact
        return out

    @staticmethod
    def _period_match(fact: dict[str, Any], *, annual: bool, is_duration: bool) -> bool:
        """True if *fact* belongs to the requested annual/quarterly period.

        Filters by filing form (10-K vs 10-Q) and, for duration facts, by the
        start→end span so a year-to-date cumulative fact never masquerades as a
        single quarter.
        """
        form = str(fact.get("form") or "")
        if annual:
            if "10-K" not in form:
                return False
        elif "10-Q" not in form:
            return False

        if is_duration:
            start = fact.get("start")
            end = fact.get("end")
            if not start or not end:
                return False
            try:
                span = (date.fromisoformat(end) - date.fromisoformat(start)).days
            except (TypeError, ValueError):
                return False
            lo, hi = _ANNUAL_SPAN if annual else _QUARTER_SPAN
            if not (lo <= span <= hi):
                return False
        return True
