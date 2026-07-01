"""Massive (formerly Massive.io) provider adapter.

Uses the Massive REST API (https://api.massive.com). Massive.io rebranded
to Massive in October 2025; the legacy ``api.polygon.io`` host and existing
keys still work in parallel, but ``api.massive.com`` is the canonical base.

Requires an API key set via the ``MASSIVE_API_KEY`` environment variable
(the legacy ``POLYGON_API_KEY`` is still honored as a fallback).

Free tier: unlimited API calls; market data is 15 minutes delayed.
Paid tiers: real-time quotes, options data, higher rate limits.

Supports: price_history, quote, info, news, corporate_actions,
options_expirations, option_chain.

Options endpoints require a Massive Options subscription; on plans
without it the API returns HTTP 403. Those are translated to
``NotSupportedError`` (not ``AUTH_ERROR``) so the router negative-caches
and skips Massive for options *without* benching it for the equity
endpoints it serves fine — cooldown is per-provider, not per-endpoint.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime
from typing import Any

import httpx

from onefinance.core.errors import ConfigError, NotSupportedError, ProviderError
from onefinance.core.models import (
    CompanyInfo,
    CorporateAction,
    NewsArticle,
    OptionChain,
    OptionContract,
    PriceBar,
    Quote,
    ScreenerResult,
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

_SOURCE = "massive"
_BASE_URL = "https://api.massive.com"

# Cap on options pagination (next_url follows) — runaway guard. 20 pages at
# 1000 contracts/page (reference) or 250 rows/page (snapshot) covers all but
# the most extreme underlyings; beyond it we log and return what we have.
_MAX_OPTION_PAGES = 20

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


class MassiveProvider(HttpProviderMixin, BaseProvider):
    """Provider adapter for Massive (formerly Massive.io).

    Parameters
    ----------
    api_key:
        Massive API key. If ``None``, reads from ``MASSIVE_API_KEY`` (or the
        legacy ``POLYGON_API_KEY``) env var.
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
        self._api_key = (
            api_key or os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY")
        )
        if not self._api_key:
            raise ConfigError(
                "MASSIVE_API_KEY not set. Set it in your environment or pass api_key="
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
        """Authenticated GET to Massive API by path. Returns decoded JSON."""
        return self._request_json(f"{self._base_url}{path}", dict(params or {}), ctx=path)

    def _get_url(self, url: str) -> Any:
        """Authenticated GET to a full Massive URL (e.g. a paginated ``next_url``).

        Massive's ``next_url`` is absolute and carries the page cursor but
        *not* the API key, so it must be re-appended.
        """
        return self._request_json(url, {}, ctx=url)

    def _request_json(self, url: str, params: dict[str, Any], ctx: str) -> Any:
        """Issue the GET, apply Massive status-code handling, return decoded JSON."""
        params["apiKey"] = self._api_key
        resp = self._request("GET", url, params=params)

        if resp.status_code == 403:
            raise ProviderError(
                code="AUTH_ERROR",
                message="Massive API key invalid or unauthorized",
                provider=self.name,
                retry_safe=False,
                http_status=403,
            )
        if resp.status_code == 404:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"Massive: resource not found for {ctx!r}",
                provider=self.name,
                retry_safe=False,
                http_status=404,
            )
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
        """Fetch OHLCV bars via the Massive Aggregates endpoint.

        Requests adjusted data (``adjusted=true``); the returned ``c`` field
        is already the adjusted close price.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        entry = _INTERVAL_MAP.get(interval)
        if entry is None:
            raise ProviderError(
                code="INVALID_ARGUMENT",
                message=f"Unsupported interval '{interval}' for Massive provider",
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
                logger.warning("Skipping Massive bar for %s: %s", sym, exc)
                continue

        return bars

    # -------------------------------------------------------------------
    # get_quote — Type B
    # -------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        """Fetch snapshot via the Massive Snapshot endpoint.

        Note: on the free (Starter) plan this data is 15 minutes delayed.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{sym}")

        ticker_data = data.get("ticker") if isinstance(data, dict) else None
        if not ticker_data:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No snapshot found for '{symbol}' via Massive",
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
                message=f"No price data in Massive snapshot for '{symbol}'",
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
        """Fetch company details via the Massive Reference Tickers endpoint."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get(f"/v3/reference/tickers/{sym}")

        results = data.get("results") if isinstance(data, dict) else None
        if not results or not isinstance(results, dict):
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No company info for '{symbol}' via Massive",
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
        """Fetch recent news via the Massive Reference News endpoint."""
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
                logger.warning("Skipping Massive news item for %s: %s", sym, exc)
                continue

        return articles

    # -------------------------------------------------------------------
    # get_corporate_actions — Type A
    # -------------------------------------------------------------------

    def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Fetch dividend and split history via Massive Reference endpoints."""
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

    # -------------------------------------------------------------------
    # Options — plan-gated (403 → NotSupportedError, not AUTH_ERROR)
    # -------------------------------------------------------------------

    def _get_options(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Fetch *all* pages of an options endpoint, mapping a plan-gap 403 to NotSupportedError.

        Massive paginates via ``next_url``; a single page silently truncates
        (liquid underlyings far exceed the per-page limit), which would make
        Massive-as-fallback return *less* data than the primary. So follow
        ``next_url`` until exhausted, capped at ``_MAX_OPTION_PAGES`` as a
        runaway guard.

        A 403 on the options API means "this plan has no options data", a
        capability gap (handled by negative caching), not a bad key. We
        translate it locally so the equity endpoints — which share Massive's
        per-provider cooldown — are never benched by an options 403.
        """
        try:
            results: list[dict[str, Any]] = []
            data = self._get(path, params)
            results.extend(data.get("results") or [])
            next_url = data.get("next_url")
            pages = 1
            while next_url and pages < _MAX_OPTION_PAGES:
                data = self._get_url(next_url)
                results.extend(data.get("results") or [])
                next_url = data.get("next_url")
                pages += 1
            if next_url:
                logger.warning(
                    "Massive options for %r hit the %d-page cap; results may be truncated",
                    path,
                    _MAX_OPTION_PAGES,
                )
            return results
        except ProviderError as exc:
            if exc.http_status == 403:
                raise NotSupportedError(self.name, path, http_status=403) from exc
            raise

    def get_options_expirations(self, symbol: str) -> list[date]:
        """Fetch available option expiration dates via the Reference Contracts endpoint.

        Paginates the full contract list (see ``_get_options``); extremely
        optioned names beyond the page cap may be truncated.
        """
        sym = normalize_symbol(symbol)
        results = self._get_options(
            "/v3/reference/options/contracts",
            {"underlying_ticker": sym, "expired": "false", "limit": 1000},
        )

        seen: set[date] = set()
        for item in results:
            exp = item.get("expiration_date")
            if not exp:
                continue
            try:
                seen.add(parse_iso_date(exp))
            except Exception:
                continue
        return sorted(seen)

    def get_option_chain(self, symbol: str, expiration: date) -> OptionChain:
        """Fetch the option chain for one expiration via the Options Snapshot endpoint.

        Paginates the snapshot so deep chains aren't clipped (see ``_get_options``).
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        results = self._get_options(
            f"/v3/snapshot/options/{sym}",
            {"expiration_date": expiration.isoformat(), "limit": 250},
        )

        calls: list[OptionContract] = []
        puts: list[OptionContract] = []
        for item in results:
            try:
                details = item.get("details") or {}
                strike = _safe_float(details.get("strike_price"))
                ticker = details.get("ticker")
                if strike is None or not ticker:
                    continue
                last_quote = item.get("last_quote") or {}
                last_trade = item.get("last_trade") or {}
                day = item.get("day") or {}
                contract = OptionContract(
                    contract_symbol=str(ticker),
                    strike=strike,
                    last_price=_safe_float(last_trade.get("price")),
                    bid=_safe_float(last_quote.get("bid")),
                    ask=_safe_float(last_quote.get("ask")),
                    volume=_safe_int(day.get("volume")),
                    open_interest=_safe_int(item.get("open_interest")),
                    implied_volatility=_safe_float(item.get("implied_volatility")),
                )
                if (details.get("contract_type") or "").lower() == "put":
                    puts.append(contract)
                else:
                    calls.append(contract)
            except Exception as exc:
                logger.warning("Skipping Massive option contract for %s: %s", sym, exc)
                continue

        calls.sort(key=lambda c: c.strike)
        puts.sort(key=lambda c: c.strike)
        return OptionChain(
            symbol=sym,
            expiration_date=expiration,
            calls=calls,
            puts=puts,
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # screen_stocks — Type A
    # -------------------------------------------------------------------

    def screen_stocks(self, query: str) -> list[ScreenerResult]:
        """Screen stocks via Massive ``/v2/reference/tickers``.

        Parses the same URL-encoded query string format as the FMP screener
        (e.g. ``'sector=Technology&exchange=NASDAQ'``).  Massive's ticker
        reference endpoint supports ``search`` (name/ticker keyword),
        ``exchange``, ``type`` (asset class), and ``market`` — financial
        metric filters (marketCap, price, volume) are not available on the
        free tier and are silently ignored.

        Param mapping:
          ``sector``  → search keyword (Massive has no sector filter)
          ``exchange`` → exchange (e.g. ``XNAS``, ``XNYS``)
          ``search``  → search
        """
        from urllib.parse import parse_qsl

        now = utc_now()
        raw_params = dict(parse_qsl(query))

        massive_params: dict[str, Any] = {
            "market": "stocks",
            "type": "CS",  # common stocks
            "active": "true",
            "limit": 50,
        }

        if raw_params.get("exchange"):
            massive_params["exchange"] = raw_params["exchange"]
        if raw_params.get("search"):
            massive_params["search"] = raw_params["search"]
        elif raw_params.get("sector"):
            # sector as keyword is imperfect but best we can do on free tier
            massive_params["search"] = raw_params["sector"]

        data = self._get("/v2/reference/tickers", massive_params)
        items = (data or {}).get("results", [])

        results: list[ScreenerResult] = []
        for item in items:
            try:
                results.append(
                    ScreenerResult(
                        symbol=item.get("ticker", ""),
                        company_name=item.get("name"),
                        market_cap=_safe_float(item.get("market_cap")),
                        sector=None,  # not available in reference endpoint
                        industry=None,
                        price=None,
                        volume=None,
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping Massive screener result: %s", exc)
                continue

        return results
