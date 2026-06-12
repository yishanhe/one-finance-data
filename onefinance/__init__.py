"""OneFinance — unified financial data API across multiple providers."""

from onefinance._version import __version__
from onefinance.core.client import OneFinanceClient
from onefinance.core.errors import (
    AllProvidersFailedError,
    FinanceError,
    NotSupportedError,
    ProviderError,
    RateLimitError,
)
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    Currency,
    DCFValuation,
    EarningsCalendarEntry,
    EarningsRecord,
    FinanceModel,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    MarketSentiment,
    NewsArticle,
    OptionChain,
    OptionContract,
    OptionsAnalytics,
    PriceBar,
    Quote,
    ScreenerResult,
    SectorInfo,
    ShortInterest,
    Symbol,
)
from onefinance.indicators import TechnicalIndicators, compute_indicators

__all__ = [
    # Client
    "OneFinanceClient",
    # Models
    "AnalystData",
    "FinanceModel",
    "PriceBar",
    "Quote",
    "IncomeStatement",
    "BalanceSheet",
    "CashFlow",
    "CompanyInfo",
    "CorporateAction",
    "DCFValuation",
    "EarningsCalendarEntry",
    "EarningsRecord",
    "FinancialRatios",
    "ForwardEstimates",
    "InsiderTrade",
    "InstitutionalHolder",
    "NewsArticle",
    "OptionChain",
    "OptionContract",
    "OptionsAnalytics",
    "ScreenerResult",
    "SectorInfo",
    "ShortInterest",
    "MarketSentiment",
    "Symbol",
    "Currency",
    # Errors
    "FinanceError",
    "ProviderError",
    "NotSupportedError",
    "RateLimitError",
    "AllProvidersFailedError",
    # Indicators
    "TechnicalIndicators",
    "compute_indicators",
    # Version
    "__version__",
]
