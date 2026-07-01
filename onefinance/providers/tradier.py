"""Tradier options-data provider adapter.

Fills the options-greeks gap: FMP / Finnhub / Twelve Data don't expose chains on
their free tiers, and yfinance gives chains but no greeks. Tradier's free Sandbox
returns full option chains with **ORATS-sourced greeks + IV** at no cost (no
brokerage account or deposit required).

Implements the two options endpoints:
  * ``get_options_expirations`` -> ``/markets/options/expirations``
  * ``get_option_chain``        -> ``/markets/options/chains`` (``greeks=true``)

Sandbox notes:
  * Quotes are 15-min delayed; greeks (courtesy of ORATS) refresh ~hourly.
  * Rate limit ~120 req/min — HTTP 429 is handled by the mixin (router cooldown
    + tier fallback), so no per-call retry loop lives here.
  * Auth is ``Authorization: Bearer <TRADIER_TOKEN>``; the token is a sandbox
    developer token from developer.tradier.com.

Defaults to the Sandbox host. Set ``TRADIER_SANDBOX=0`` (or ``false``) to target
the production host with the same token (e.g. on a Tradier Pro plan for real-time
data).
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

import httpx

from onefinance.core.errors import ConfigError, ProviderError
from onefinance.core.models import OptionChain, OptionContract, Quote
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

_SOURCE = "tradier"
_PROD_BASE = "https://api.tradier.com/v1"
_SANDBOX_BASE = "https://sandbox.tradier.com/v1"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


class TradierProvider(HttpProviderMixin, BaseProvider):
    """Provider adapter for Tradier (options chains with ORATS greeks).

    Parameters
    ----------
    api_key:
        Tradier token. If ``None``, reads from ``TRADIER_TOKEN`` env var.
    timeout:
        HTTP request timeout in seconds.
    base_url:
        Override the base URL. If ``None``, defaults to the Sandbox host unless
        ``TRADIER_SANDBOX`` is set falsy, in which case the production host.
    http_client:
        Optional shared ``httpx.Client``.
    """

    name = _SOURCE

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 10,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("TRADIER_TOKEN")
        if not self._api_key:
            raise ConfigError("TRADIER_TOKEN not set. Set it in your environment or pass api_key=")
        if base_url is not None:
            self._base_url = base_url
        else:
            # Default to sandbox (free, keyless of brokerage); env can opt into prod.
            sandbox = "TRADIER_SANDBOX" not in os.environ or _truthy(
                os.environ.get("TRADIER_SANDBOX")
            )
            self._base_url = _SANDBOX_BASE if sandbox else _PROD_BASE
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

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        """Authenticated GET to the Tradier API. Returns decoded JSON.

        429 is raised as ``RateLimitError`` by the mixin (router handles the
        cooldown + tier fallback); 401 is a bad/expired token.
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        resp = self._request("GET", f"{self._base_url}{path}", params=params, headers=headers)

        if resp.status_code in (401, 403):
            raise ProviderError(
                code="AUTH_ERROR",
                message="Tradier token invalid or unauthorized",
                provider=self.name,
                retry_safe=False,
                http_status=resp.status_code,
            )
        self._raise_for_status(resp)
        return resp.json()

    # -------------------------------------------------------------------
    # get_quote — Type B (15-min delayed on Sandbox)
    # -------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        """Fetch quote via ``/markets/quotes``.

        Tradier Sandbox returns 15-min delayed quotes.  The ``last`` price
        is the most recent trade; ``change`` and ``change_percentage`` are
        intraday vs previous close.
        """
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get("/markets/quotes", {"symbols": sym, "greeks": "false"})
        node = (data or {}).get("quotes") or {}
        q = node.get("quote") or {}
        # bare dict when single symbol; list otherwise — take first
        if isinstance(q, list):
            q = q[0] if q else {}

        price_raw = q.get("last") or q.get("ask") or q.get("bid")
        if not price_raw:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"Tradier returned no quote for '{symbol}'",
                provider=self.name,
                retry_safe=False,
            )

        ts_raw = q.get("trade_date")
        try:
            from datetime import UTC

            ts = (
                __import__("datetime").datetime.fromtimestamp(float(ts_raw) / 1000, tz=UTC)
                if ts_raw
                else now
            )
        except Exception:
            ts = now

        return Quote(
            symbol=sym,
            timestamp=ts,
            price=float(price_raw),
            bid=_safe_float(q.get("bid")),
            ask=_safe_float(q.get("ask")),
            volume=_safe_int(q.get("volume")),
            source=self.name,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # get_options_expirations — Type A
    # -------------------------------------------------------------------

    def get_options_expirations(self, symbol: str) -> list[date]:
        """Available option expiration dates via the Expirations endpoint."""
        sym = normalize_symbol(symbol)
        data = self._get(
            "/markets/options/expirations",
            {"symbol": sym, "includeAllRoots": "true"},
        )

        node = (data or {}).get("expirations") or {}
        raw = node.get("date") or []
        if isinstance(raw, str):  # a single expiration comes back as a scalar
            raw = [raw]

        seen: set[date] = set()
        for d in raw:
            try:
                seen.add(parse_iso_date(d))
            except Exception:
                continue
        return sorted(seen)

    # -------------------------------------------------------------------
    # get_option_chain — Type A
    # -------------------------------------------------------------------

    def get_option_chain(self, symbol: str, expiration: date) -> OptionChain:
        """Full option chain (with ORATS greeks) for one expiration."""
        now = utc_now()
        sym = normalize_symbol(symbol)

        data = self._get(
            "/markets/options/chains",
            {"symbol": sym, "expiration": expiration.isoformat(), "greeks": "true"},
        )

        # ``options`` is null when no contracts exist; ``option`` is a bare dict
        # (not a list) when the chain has exactly one contract.
        options_node = (data or {}).get("options")
        rows: list[dict[str, Any]] = []
        if options_node:
            opt = options_node.get("option") or []
            if isinstance(opt, dict):
                opt = [opt]
            rows = opt

        calls: list[OptionContract] = []
        puts: list[OptionContract] = []
        for row in rows:
            try:
                strike = _safe_float(row.get("strike"))
                ticker = row.get("symbol")
                if strike is None or not ticker:
                    continue
                greeks = row.get("greeks") or {}
                contract = OptionContract(
                    contract_symbol=str(ticker),
                    strike=strike,
                    last_price=_safe_float(row.get("last")),
                    bid=_safe_float(row.get("bid")),
                    ask=_safe_float(row.get("ask")),
                    volume=_safe_int(row.get("volume")),
                    open_interest=_safe_int(row.get("open_interest")),
                    implied_volatility=_safe_float(greeks.get("mid_iv")),
                    delta=_safe_float(greeks.get("delta")),
                    gamma=_safe_float(greeks.get("gamma")),
                    theta=_safe_float(greeks.get("theta")),
                    vega=_safe_float(greeks.get("vega")),
                    rho=_safe_float(greeks.get("rho")),
                    smv_vol=_safe_float(greeks.get("smv_vol")),
                )
                if (row.get("option_type") or "").lower() == "put":
                    puts.append(contract)
                else:
                    calls.append(contract)
            except Exception as exc:
                logger.warning("Skipping Tradier option contract for %s: %s", sym, exc)
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
