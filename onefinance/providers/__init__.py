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


def _build_massive(cfg: ProviderConfig, http_client: httpx.Client | None) -> BaseProvider | None:
    if not cfg.api_key:
        return None
    from onefinance.providers.massive import MassiveProvider

    return MassiveProvider(api_key=cfg.api_key, timeout=cfg.timeout_s, http_client=http_client)


def _build_tradier(cfg: ProviderConfig, http_client: httpx.Client | None) -> BaseProvider | None:
    if not cfg.api_key:
        return None
    from onefinance.providers.tradier import TradierProvider

    return TradierProvider(api_key=cfg.api_key, timeout=cfg.timeout_s, http_client=http_client)


def _build_edgar(cfg: ProviderConfig, http_client: httpx.Client | None) -> BaseProvider | None:
    # SEC EDGAR needs no API key; always available.
    from onefinance.providers.edgar import SecEdgarProvider

    return SecEdgarProvider(timeout=cfg.timeout_s, http_client=http_client)


def _build_cboe(cfg: ProviderConfig, http_client: httpx.Client | None) -> BaseProvider | None:
    # Cboe delayed quotes need no API key.
    from onefinance.providers.cboe import CboeProvider

    return CboeProvider(timeout=cfg.timeout_s, http_client=http_client)


register(ProviderSpec("fmp", _build_fmp, requires_api_key=True))
register(ProviderSpec("finnhub", _build_finnhub, requires_api_key=True))
register(ProviderSpec("twelve_data", _build_twelve_data, requires_api_key=True))
register(ProviderSpec("yfinance", _build_yfinance, requires_api_key=False))
register(ProviderSpec("alpha_vantage", _build_alpha_vantage, requires_api_key=True))
register(ProviderSpec("massive", _build_massive, requires_api_key=True))
register(ProviderSpec("tradier", _build_tradier, requires_api_key=True))
register(ProviderSpec("edgar", _build_edgar, requires_api_key=False))
register(ProviderSpec("cboe", _build_cboe, requires_api_key=False))
