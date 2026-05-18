"""Twelve Data provider adapter.

Uses the Twelve Data REST API (https://api.twelvedata.com).
Requires an API key set via the ``TWELVE_DATA_API_KEY`` environment variable.

Free tier: 800 calls/day, 8 calls/minute.
Supports price_history and quote only (see design doc §3 capability matrix).

See design doc §7 for rate-limit detection details.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime
from typing import Any

import httpx

from onefinance.core.errors import ConfigError, ProviderError
from onefinance.core.models import PriceBar, Quote
from onefinance.providers._http import HttpProviderMixin
from onefinance.providers._utils import (
    normalize_symbol,
    parse_iso_date,
    parse_iso_datetime_utc,
    utc_now,
)
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "twelve_data"
_BASE_URL = "https://api.twelvedata.com"


class TwelveDataProvider(HttpProviderMixin, BaseProvider):
    """Provider adapter for Twelve Data (price history and quotes only).

    Parameters
    ----------
    api_key:
        Twelve Data API key. If ``None``, reads from ``TWELVE_DATA_API_KEY`` env var.
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
        self._api_key = api_key or os.environ.get("TWELVE_DATA_API_KEY")
        if not self._api_key:
            raise ConfigError(
                "TWELVE_DATA_API_KEY not set. Set it in your environment or pass api_key="
            )
        self._base_url = base_url
        super().__init__(timeout=float(timeout), http_client=http_client)

    # -------------------------------------------------------------------
    # Rate-limit signals — adds Twelve Data quirks (JSON envelope code:429)
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
        if isinstance(data, dict) and data.get("code") == 429:
            return True, None
        return False, None

    # -------------------------------------------------------------------
    # HTTP helper
    # -------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Authenticated GET to Twelve Data API. Returns decoded JSON."""
        url = f"{self._base_url}/{path}"
        req_params = dict(params or {})
        req_params["apikey"] = self._api_key

        resp = self._request("GET", url, params=req_params)

        if resp.status_code != 200:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"Twelve Data HTTP {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                retry_safe=resp.status_code >= 500,
            )

        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "error":
            raise ProviderError(
                code="PROVIDER_ERROR",
                message=f"Twelve Data error: {data.get('message', '')}",
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
        """Fetch OHLCV bars via ``/time_series``."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        resolution_map = {
            "1m": "1min",
            "5m": "5min",
            "15m": "15min",
            "30m": "30min",
            "1h": "1h",
            "60m": "1h",
            "1d": "1day",
            "1wk": "1week",
            "1mo": "1month",
        }
        res = resolution_map.get(interval, "1day")

        data = self._get(
            "time_series",
            params={
                "symbol": sym,
                "interval": res,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "outputsize": 5000,
                "order": "ASC",
            },
        )

        values = data.get("values", []) if isinstance(data, dict) else []
        if not values:
            return []

        bars: list[PriceBar] = []
        for item in values:
            try:
                dt_str = item["datetime"]
                if len(dt_str) > 10:
                    bar_ts = parse_iso_datetime_utc(dt_str)
                    bar_date = bar_ts.date()
                else:
                    bar_date = parse_iso_date(dt_str)
                    bar_ts = None

                bars.append(
                    PriceBar(
                        symbol=sym,
                        date=bar_date,
                        timestamp=bar_ts,
                        open=float(item["open"]),
                        high=float(item["high"]),
                        low=float(item["low"]),
                        close=float(item["close"]),
                        adj_close=float(item.get("adjusted_close", item["close"])),
                        volume=int(float(item.get("volume", 0))),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping Twelve Data bar for %s: %s", sym, exc)
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

        if not data or not isinstance(data, dict) or not data.get("close"):
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No quote found for '{symbol}' via Twelve Data",
                provider=self.name,
                retry_safe=False,
            )

        ts = data.get("timestamp")
        try:
            timestamp = datetime.fromtimestamp(float(ts), tz=UTC) if ts else now
        except (ValueError, TypeError):
            timestamp = now

        return Quote(
            symbol=sym,
            timestamp=timestamp,
            price=float(data["close"]),
            bid=None,
            ask=None,
            volume=int(float(data.get("volume", 0))),
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # Rate-limit detection
    # -------------------------------------------------------------------

    def is_rate_limited(self, response: Any) -> bool:
        if isinstance(response, httpx.Response):
            if response.status_code == 429:
                return True
            try:
                matched: bool = response.json().get("code") == 429
                return matched
            except Exception:
                return False
        if isinstance(response, Exception):
            return "429" in str(response)
        return False

    def cooldown_for(self, response: Any) -> float:
        return 60.0
