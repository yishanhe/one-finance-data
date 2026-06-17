"""Providers subpackage — registers built-in provider specs on import.

Importing this package has the side effect of populating
``onefinance.providers._factory._REGISTRY`` with the four built-in providers.
Each spec uses a lazy builder so missing optional dependencies (or absent
API keys) never break package import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from onefinance.core.config import ProviderConfig
from onefinance.providers._factory import ProviderSpec, register
from onefinance.providers.base import BaseProvider

if TYPE_CHECKING:
    import httpx


def _build_fmp(cfg: ProviderConfig, http_client: httpx.Client | None) -> BaseProvider | None:
    if not cfg.api_key:
        return None
    from onefinance.providers.fmp import FMPProvider

    return FMPProvider(api_key=cfg.api_key, timeout=cfg.timeout_s, http_client=http_client)


def _build_finnhub(cfg: ProviderConfig, http_client: httpx.Client | None) -> BaseProvider | None:
    if not cfg.api_key:
        return None
    from onefinance.providers.finnhub import FinnhubProvider

    return FinnhubProvider(api_key=cfg.api_key, timeout=cfg.timeout_s, http_client=http_client)


def _build_twelve_data(
    cfg: ProviderConfig, http_client: httpx.Client | None
) -> BaseProvider | None:
    if not cfg.api_key:
        return None
    from onefinance.providers.twelve_data import TwelveDataProvider

    return TwelveDataProvider(api_key=cfg.api_key, timeout=cfg.timeout_s, http_client=http_client)


def _build_yfinance(cfg: ProviderConfig, http_client: httpx.Client | None) -> BaseProvider | None:
    # yfinance does not use httpx directly; ignore the http_client kwarg.
    del http_client
    from onefinance.providers.yfinance_provider import YFinanceProvider

    return YFinanceProvider(timeout=cfg.timeout_s)


def _build_alpha_vantage(
    cfg: ProviderConfig, http_client: httpx.Client | None
) -> BaseProvider | None:
    if not cfg.api_key:
        return None
    from onefinance.providers.alpha_vantage import AlphaVantageProvider

    return AlphaVantageProvider(api_key=cfg.api_key, timeout=cfg.timeout_s, http_client=http_client)


def _build_polygon(cfg: ProviderConfig, http_client: httpx.Client | None) -> BaseProvider | None:
    if not cfg.api_key:
        return None
    from onefinance.providers.polygon import PolygonProvider

    return PolygonProvider(api_key=cfg.api_key, timeout=cfg.timeout_s, http_client=http_client)


register(ProviderSpec("fmp", _build_fmp, requires_api_key=True))
register(ProviderSpec("finnhub", _build_finnhub, requires_api_key=True))
register(ProviderSpec("twelve_data", _build_twelve_data, requires_api_key=True))
register(ProviderSpec("yfinance", _build_yfinance, requires_api_key=False))
register(ProviderSpec("alpha_vantage", _build_alpha_vantage, requires_api_key=True))
register(ProviderSpec("polygon", _build_polygon, requires_api_key=True))
