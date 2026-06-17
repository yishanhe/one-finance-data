"""Polygon.io provider adapter.

Uses the Polygon REST API (https://api.polygon.io).
Requires an API key set via the ``POLYGON_API_KEY`` environment variable.

Free tier: unlimited API calls; market data is 15 minutes delayed.
Paid tiers: real-time quotes, options data, higher rate limits.

Supports: price_history, quote, info, news, corporate_actions.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime
from typing import Any

import httpx

from onefinance.core.errors import ConfigError, ProviderError
from onefinance.core.models import (
    CompanyInfo,
    CorporateAction,
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
    parse_iso_datetime_utc,
    utc_now,
)
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "polygon"
_BASE_URL = "https://api.polygon.io"

_INTERVAL_MAP: dict[str, tuple[str, int]] = {
    "1m": ("minute", 1),
    "5m": ("minute", 5),
    "15m": ("minute", 15),
    "30m": ("minute", 30),
    "1h": ("hour", 1),
    "60m": ("hour", 1),
    "1d": ("day", 1),
    "1wk": ("week", 1),
    "1mo": ("month", 1),
}


class PolygonProvider(HttpProviderMixin, BaseProvider):
    """Provider adapter for Polygon.io.

    Parameters
    ----------
    api_key:
        Polygon API key. If ``None``, reads from ``POLYGON_API_KEY`` env var.
    timeout:
        HTTP request timeout in seconds.
    base_url:
        Override the base URL (useful for testing).
    http_client:
        Optional shared ``httpx.Client``.
    """

    name = _SOURCE

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 10,
        base_url: str = _BASE_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("POLYGON_API_KEY")
        if not self._api_key:
            raise ConfigError(
                "POLYGON_API_KEY not set. Set it in your environment or pass api_key="
            )
        self._base_url = base_url
        super().__init__(timeout=float(timeout), http_client=http_client)

    # -------------------------------------------------------------------
    # Rate-limit interface (required by BaseProvider ABC)
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
    # HTTP helper
    # -------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Authenticated GET to Polygon API. Returns decoded JSON."""
        url = f"{self._base_url}{path}"
        req_params = dict(params or {})
        req_params["apiKey"] = self._api_key

        resp = self._request("GET", url, params=req_params)

        if resp.status_code == 403:
            raise ProviderError(
                code="AUTH_ERROR",
                message="Polygon API key invalid or unauthorized",
                provider=self.name,
                retry_safe=False,
                http_status=403,
            )
        if resp.status_code == 404:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"Polygon: resource not found for {path!r}",
                provider=self.name,
                retry_safe=False,
                http_status=404,
            )
        if resp.status_code != 200:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"Polygon HTTP {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                retry_safe=resp.status_code >= 500,
                http_status=resp.status_code,
            )

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
        """Fetch OHLCV bars via the Polygon Aggregates endpoint.

        Requests adjusted data (``adjusted=true``); the returned ``c`` field
        is already the adjusted close price.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        entry = _INTERVAL_MAP.get(interval)
        if entry is None:
            raise ProviderError(
                code="INVALID_ARGUMENT",
                message=f"Unsupported interval '{interval}' for Polygon provider",
                provider=self.name,
                retry_safe=False,
            )
        timespan, multiplier = entry

        path = (
            f"/v2/aggs/ticker/{sym}/range/{multiplier}/{timespan}"
            f"/{start.isoformat()}/{end.isoformat()}"
        )
        data = self._get(path, params={"adjusted": "true", "sort": "asc", "limit": 50000})

        results = data.get("results") if isinstance(data, dict) else None
        if not results:
            return []

        bars: list[PriceBar] = []
        for item in results:
            try:
                ts_ms = item["t"]
                bar_dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
                bars.append(
                    PriceBar(
                        symbol=sym,
                        date=bar_dt.date(),
                        timestamp=bar_dt if timespan != "day" else None,
                        open=float(item["o"]),
                        high=float(item["h"]),
                        low=float(item["l"]),
                        close=float(item["c"]),
                        adj_close=float(item["c"]),
                        volume=int(float(item.get("v", 0))),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping Polygon bar for %s: %s", sym, exc)
                continue

        return bars

    # -------------------------------------------------------------------
    # get_quote — Type B
    # -------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        """Fetch snapshot via the Polygon Snapshot endpoint.

        Note: on the free (Starter) plan this data is 15 minutes delayed.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{sym}")

        ticker_data = data.get("ticker") if isinstance(data, dict) else None
        if not ticker_data:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No snapshot found for '{symbol}' via Polygon",
                provider=self.name,
                retry_safe=False,
            )

        last_trade = ticker_data.get("lastTrade") or {}
        last_quote_data = ticker_data.get("lastQuote") or {}
        day = ticker_data.get("day") or {}

        price = _safe_float(last_trade.get("p")) or _safe_float(day.get("c"))
        if not price:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No price data in Polygon snapshot for '{symbol}'",
                provider=self.name,
                retry_safe=False,
            )

        # lastTrade.t is nanoseconds; ticker.updated is milliseconds
        trade_ns = last_trade.get("t")
        if trade_ns:
            try:
                timestamp = datetime.fromtimestamp(trade_ns / 1_000_000_000, tz=UTC)
            except Exception:
                timestamp = now
        else:
            updated_ms = ticker_data.get("updated")
            if updated_ms:
                try:
                    timestamp = datetime.fromtimestamp(updated_ms / 1000, tz=UTC)
                except Exception:
                    timestamp = now
            else:
                timestamp = now

        return Quote(
            symbol=sym,
            timestamp=timestamp,
            price=price,
            bid=_safe_float(last_quote_data.get("p")),
            ask=_safe_float(last_quote_data.get("P")),
            volume=int(_safe_float(day.get("v")) or 0),
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # get_info — Type A
    # -------------------------------------------------------------------

    def get_info(self, symbol: str) -> CompanyInfo:
        """Fetch company details via the Polygon Reference Tickers endpoint."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get(f"/v3/reference/tickers/{sym}")

        results = data.get("results") if isinstance(data, dict) else None
        if not results or not isinstance(results, dict):
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No company info for '{symbol}' via Polygon",
                provider=self.name,
                retry_safe=False,
            )

        raw_currency = results.get("currency_name") or ""
        currency: str | None = raw_currency.upper()[:3] if len(raw_currency) >= 3 else None

        locale = (results.get("locale") or "").upper() or None

        return CompanyInfo(
            symbol=sym,
            name=results.get("name") or symbol,
            exchange=results.get("primary_exchange"),
            sector=None,
            industry=results.get("sic_description"),
            country=locale,
            market_cap=_safe_float(results.get("market_cap")),
            beta=None,
            shares_outstanding=_safe_int(results.get("share_class_shares_outstanding")),
            description=results.get("description"),
            website=results.get("homepage_url"),
            employees=_safe_int(results.get("total_employees")),
            currency=currency,
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # get_news — Type A
    # -------------------------------------------------------------------

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        """Fetch recent news via the Polygon Reference News endpoint."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get(
            "/v2/reference/news",
            params={"ticker": sym, "limit": limit, "sort": "published_utc", "order": "desc"},
        )

        articles: list[NewsArticle] = []
        for item in data.get("results") or []:
            try:
                publisher_obj = item.get("publisher") or {}
                publisher = publisher_obj.get("name") or "Unknown"
                title = item.get("title") or ""
                url = item.get("article_url") or ""
                if not title or not url:
                    continue
                published_str = item.get("published_utc", "")
                try:
                    published_at = parse_iso_datetime_utc(published_str)
                except Exception:
                    published_at = now
                articles.append(
                    NewsArticle(
                        symbol=sym,
                        title=title,
                        publisher=publisher,
                        link=url,
                        published_at=published_at,
                        summary=item.get("description"),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping Polygon news item for %s: %s", sym, exc)
                continue

        return articles

    # -------------------------------------------------------------------
    # get_corporate_actions — Type A
    # -------------------------------------------------------------------

    def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Fetch dividend and split history via Polygon Reference endpoints."""
        now = utc_now()
        sym = normalize_symbol(symbol)
        actions: list[CorporateAction] = []

        div_data = self._get("/v3/reference/dividends", params={"ticker": sym, "limit": 1000})
        for item in div_data.get("results") or []:
            try:
                ex_date = item.get("ex_dividend_date")
                if not ex_date:
                    continue
                actions.append(
                    CorporateAction(
                        symbol=sym,
                        date=parse_iso_date(ex_date),
                        action_type="dividend",
                        amount=_safe_float(item.get("cash_amount")),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception:
                continue

        split_data = self._get("/v3/reference/splits", params={"ticker": sym, "limit": 1000})
        for item in split_data.get("results") or []:
            try:
                exec_date = item.get("execution_date")
                if not exec_date:
                    continue
                split_from = float(item.get("split_from", 1) or 1)
                split_to = float(item.get("split_to", 1) or 1)
                ratio = split_to / split_from if split_from != 0 else 0.0
                actions.append(
                    CorporateAction(
                        symbol=sym,
                        date=parse_iso_date(exec_date),
                        action_type="split",
                        split_ratio=ratio,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception:
                continue

        return sorted(actions, key=lambda a: a.date, reverse=True)
