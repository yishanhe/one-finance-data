"""Abstract base class for all financial data providers.

Each provider implements the endpoints it supports and leaves the rest
as the default ``NotSupportedError``-raising stubs.  Capability is
discoverable at runtime via ``provider.supports("ratios")``.

See design doc §9 for the full interface contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from onefinance.core.errors import NotSupportedError
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    DCFValuation,
    EarningsCalendarEntry,
    EarningsRecord,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    MarketSentiment,
    NewsArticle,
    OptionChain,
    PeerCompany,
    PriceBar,
    Quote,
    ScreenerResult,
    SectorInfo,
    ShortInterest,
)

# Maps endpoint name → method name for capability discovery
_ENDPOINT_METHODS: dict[str, str] = {
    "price_history": "get_price_history",
    "quote": "get_quote",
    "quotes": "get_quotes",
    "financials": "get_financials",
    "info": "get_info",
    "ratios": "get_ratios",
    "earnings": "get_earnings",
    "insider_trades": "get_insider_trades",
    "dcf": "get_dcf",
    "news": "get_news",
    "corporate_actions": "get_corporate_actions",
    "institutional_holders": "get_institutional_holders",
    "analyst_data": "get_analyst_data",
    "options_expirations": "get_options_expirations",
    "option_chain": "get_option_chain",
    "forward_estimates": "get_forward_estimates",
    "screen_stocks": "screen_stocks",
    "sector_overview": "get_sector_overview",
    "earnings_calendar": "get_earnings_calendar",
    "short_interest": "get_short_interest",
    "market_sentiment": "get_market_sentiment",
    "peers": "get_peers",
}


class BaseProvider(ABC):
    """Abstract base for financial data provider adapters.

    Subclasses must set ``name`` and override the endpoint methods they
    support.  Default implementations raise ``NotSupportedError`` so the
    router can transparently skip unsupported endpoints.

    Subclasses must also implement ``is_rate_limited()`` and
    ``cooldown_for()`` so the router can handle quota exhaustion
    generically.
    """

    name: str

    # -------------------------------------------------------------------
    # Data endpoint defaults — override in subclass to add support
    # -------------------------------------------------------------------

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[PriceBar]:
        """Fetch OHLCV bars for *symbol* between *start* and *end*."""
        raise NotSupportedError(self.name, "price_history")

    def get_quote(self, symbol: str) -> Quote:
        """Fetch current quote for *symbol*."""
        raise NotSupportedError(self.name, "quote")

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        """Fetch current quotes for a list of *symbols*.

        Providers that natively support batching should override this.
        The default implementation falls back to concurrent single requests.
        """
        import concurrent.futures

        # If it doesn't even support single quote, fail early.
        if not self.supports("quote"):
            raise NotSupportedError(self.name, "quotes")

        quotes = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(symbols), 10)) as executor:
            # We want to preserve order, so use executor.map
            for quote in executor.map(self.get_quote, symbols):
                quotes.append(quote)
        return quotes

    def get_financials(
        self,
        symbol: str,
        statement: str,
        period: str,
    ) -> list[IncomeStatement | BalanceSheet | CashFlow]:
        """Fetch financial statements for *symbol*."""
        raise NotSupportedError(self.name, "financials")

    def get_info(self, symbol: str) -> CompanyInfo:
        """Fetch company profile information for *symbol*."""
        raise NotSupportedError(self.name, "info")

    def get_ratios(
        self,
        symbol: str,
        period: str,
    ) -> list[FinancialRatios]:
        """Fetch financial ratios for *symbol*."""
        raise NotSupportedError(self.name, "ratios")

    def get_earnings(self, symbol: str) -> list[EarningsRecord]:
        """Fetch earnings records for *symbol*."""
        raise NotSupportedError(self.name, "earnings")

    def get_insider_trades(
        self,
        symbol: str,
        since: date | None = None,
    ) -> list[InsiderTrade]:
        """Fetch insider trades for *symbol*."""
        raise NotSupportedError(self.name, "insider_trades")

    def get_dcf(self, symbol: str) -> DCFValuation:
        """Fetch DCF valuation for *symbol*."""
        raise NotSupportedError(self.name, "dcf")

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        """Fetch recent news articles for *symbol*."""
        raise NotSupportedError(self.name, "news")

    def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Fetch dividend and split history for *symbol*."""
        raise NotSupportedError(self.name, "corporate_actions")

    def get_institutional_holders(self, symbol: str) -> list[InstitutionalHolder]:
        """Fetch top institutional holders for *symbol*."""
        raise NotSupportedError(self.name, "institutional_holders")

    def get_analyst_data(self, symbol: str) -> AnalystData:
        """Fetch analyst price targets and ratings for *symbol*."""
        raise NotSupportedError(self.name, "analyst_data")

    def get_forward_estimates(self, symbol: str) -> list[ForwardEstimates]:
        """Fetch forward-looking consensus estimates for future periods."""
        raise NotSupportedError(self.name, "forward_estimates")

    def get_options_expirations(self, symbol: str) -> list[date]:
        """Fetch available option expiration dates for *symbol*."""
        raise NotSupportedError(self.name, "options_expirations")

    def get_option_chain(self, symbol: str, expiration: date) -> OptionChain:
        """Fetch the option chain for *symbol* and *expiration*."""
        raise NotSupportedError(self.name, "option_chain")

    def screen_stocks(self, query: str) -> list[ScreenerResult]:
        """Screen stocks based on a provider-specific query string."""
        raise NotSupportedError(self.name, "screen_stocks")

    def get_sector_overview(self, sector: str) -> SectorInfo:
        """Fetch overview for a specific sector."""
        raise NotSupportedError(self.name, "sector_overview")

    def get_earnings_calendar(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> list[EarningsCalendarEntry]:
        """Fetch scheduled earnings releases for a date range."""
        raise NotSupportedError(self.name, "earnings_calendar")

    def get_short_interest(self, symbol: str) -> ShortInterest:
        """Fetch short interest data for *symbol*."""
        raise NotSupportedError(self.name, "short_interest")

    def get_market_sentiment(self) -> MarketSentiment:
        """Fetch market-wide put/call ratio data."""
        raise NotSupportedError(self.name, "market_sentiment")

    def get_peers(self, symbol: str) -> list[PeerCompany]:
        """Fetch peer/comparable companies for *symbol*."""
        raise NotSupportedError(self.name, "peers")

    # -------------------------------------------------------------------
    # Rate-limit handling — must be implemented by every provider
    # -------------------------------------------------------------------

    @abstractmethod
    def is_rate_limited(self, response: Any) -> bool:
        """Return ``True`` if *response* indicates a rate-limit hit."""

    @abstractmethod
    def cooldown_for(self, response: Any) -> float:
        """Seconds to wait before retrying this provider."""

    # -------------------------------------------------------------------
    # Capability discovery
    # -------------------------------------------------------------------

    def supports(self, endpoint: str) -> bool:
        """Return ``True`` if this provider overrides the given endpoint.

        Works by checking whether the method on this instance is
        different from the default stub on ``BaseProvider``.
        """
        method_name = _ENDPOINT_METHODS.get(endpoint)
        if method_name is None:
            return False
        # If the method on the subclass is the same object as on
        # BaseProvider, it hasn't been overridden.
        own_method = getattr(type(self), method_name, None)
        base_method = getattr(BaseProvider, method_name, None)
        return own_method is not base_method

    @property
    def supported_endpoints(self) -> list[str]:
        """List of endpoint names this provider supports."""
        return [ep for ep in _ENDPOINT_METHODS if self.supports(ep)]
