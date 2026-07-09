"""Cboe delayed quote provider for volatility indexes.

Cboe is used here only for index symbols that equity quote providers often
return as zero or missing (for example VIX3M/VXN/RVX). It is intentionally
narrow: ordinary equities raise ``NotSupportedError`` immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import httpx

from onefinance.core.errors import NotSupportedError, ProviderError
from onefinance.core.models import Quote
from onefinance.providers._http import HttpProviderMixin
from onefinance.providers._utils import _safe_float, _safe_int, change_pct_from_prev_close, utc_now
from onefinance.providers.base import BaseProvider

_SOURCE = "cboe"
_BASE_URL = "https://cdn.cboe.com/api/global/delayed_quotes"

_SUPPORTED_SYMBOLS = {
    "VIX": "_VIX",
    "VIX3M": "_VIX3M",
    "VXSMH": "_VXSMH",
    "VXN": "_VXN",
    "RVX": "_RVX",
}


class CboeProvider(HttpProviderMixin, BaseProvider):
    """Provider adapter for Cboe delayed volatility-index quotes."""

    name = _SOURCE
    _default_rate_limit_cooldown_s = 60.0

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        http_client: httpx.Client | None = None,
        base_url: str = _BASE_URL,
    ) -> None:
        super().__init__(timeout=timeout, http_client=http_client)
        self._base_url = base_url.rstrip("/")

    def get_quote(self, symbol: str) -> Quote:
        """Fetch a delayed quote for a supported volatility-index symbol."""
        now = utc_now()
        sym = symbol.strip().upper()
        # Accept both bare and Yahoo-style caret forms (VIX and ^VIX).
        cboe_symbol = _SUPPORTED_SYMBOLS.get(sym.lstrip("^"))
        if cboe_symbol is None:
            raise NotSupportedError(self.name, "quote")

        data = self._get_json(f"quotes/{cboe_symbol}.json")
        item = _quote_payload(data, cboe_symbol)
        price = _first_number(
            item,
            "last",
            "last_price",
            "current_price",
            "price",
            "close",
            "value",
        )
        if price is None or price <= 0:
            raise ProviderError(
                code="DATA_NOT_FOUND",
                message=f"No valid Cboe quote found for '{symbol}'",
                provider=self.name,
                retry_safe=False,
            )

        # Cboe's `prev_day_close` is unreliable — after settlement it echoes the
        # *current* close, which made change_pct read 0.0. `price_change` /
        # `price_change_percent` are authoritative, so derive prev_close from
        # the change and only fall back to the close-like keys without it.
        change_abs = _first_number(item, "price_change")
        change_pct = _first_number(item, "price_change_percent")
        prev_close: float | None
        if change_abs is not None:
            prev_close = price - change_abs
        else:
            prev_close = _first_number(
                item,
                "prev_close",
                "previous_close",
                "previousClose",
                "prev_day_close",
            )
        if change_pct is None:
            change_pct = change_pct_from_prev_close(price, prev_close)
        timestamp = _parse_timestamp(
            _first_value(item, "timestamp", "updated", "last_trade_time", "trade_time")
        )

        return Quote(
            symbol=sym,
            timestamp=timestamp or now,
            price=price,
            bid=_first_number(item, "bid"),
            ask=_first_number(item, "ask"),
            volume=_first_int(item, "volume", "total_volume"),
            prev_close=prev_close,
            change_pct=change_pct,
            source=_SOURCE,
            fetched_at=now,
        )

    def _get_json(self, path: str) -> Any:
        url = f"{self._base_url}/{path.lstrip('/')}"
        resp = self._request("GET", url)
        if resp.status_code == 403 or resp.status_code == 404:
            # Cboe returns 403/404 for some missing index JSON paths. Keep this
            # symbol-scoped; a path miss for one index should not globally
            # negative-cache the whole Cboe quote endpoint.
            raise NotSupportedError(self.name, "quote")
        self._raise_for_status(resp)
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(
                code="PROVIDER_ERROR",
                message=f"Cboe returned non-JSON quote response: {resp.text[:120]}",
                provider=self.name,
                retry_safe=True,
                http_status=resp.status_code,
            ) from exc

    def is_rate_limited(self, response: Any) -> bool:
        return isinstance(response, httpx.Response) and response.status_code == 429

    def cooldown_for(self, response: Any) -> float:
        return 60.0


def _quote_payload(data: Any, symbol: str) -> dict[str, Any]:
    if isinstance(data, dict):
        for key in ("data", "quote", "quotes"):
            nested = data.get(key)
            if isinstance(nested, dict):
                for nested_symbol, payload in nested.items():
                    if _same_symbol(str(nested_symbol), symbol) and isinstance(payload, dict):
                        return _with_parent_timestamp(cast("dict[str, Any]", payload), data)
                return _with_parent_timestamp(nested, data)
            if isinstance(nested, list):
                return _with_parent_timestamp(_first_matching(nested, symbol), data)
        return data
    if isinstance(data, list):
        return _first_matching(data, symbol)
    return {}


def _first_matching(items: list[Any], symbol: str) -> dict[str, Any]:
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_symbol = str(item.get("symbol") or item.get("ticker") or "")
        if _same_symbol(raw_symbol, symbol):
            return item
    for item in items:
        if isinstance(item, dict):
            return item
    return {}


def _with_parent_timestamp(item: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    parent_timestamp = parent.get("timestamp")
    if parent_timestamp is None or item.get("timestamp") is not None:
        return item
    return {**item, "timestamp": parent_timestamp}


def _same_symbol(left: str, right: str) -> bool:
    return _symbol_key(left) == _symbol_key(right)


def _symbol_key(symbol: str) -> str:
    return symbol.upper().lstrip("_^")


def _first_value(data: dict[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _first_number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _safe_float(data.get(key))
        if value is not None:
            return value
    return None


def _first_int(data: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = _safe_int(data.get(key))
        if value is not None:
            return value
    return 0


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None
