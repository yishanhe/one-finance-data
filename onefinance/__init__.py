"""OneFinance — unified financial data API across multiple providers."""

from onefinance.core.client import OneFinanceClient
from onefinance.core.errors import (
    AllProvidersFailedError,
    FinanceError,
    NotSupportedError,
    ProviderError,
    RateLimitError,
)
from onefinance.indicators import TechnicalIndicators, compute_indicators
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    Currency,
    DCFValuation,
    EarningsRecord,
    FinancialRatios,
    FinanceModel,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    NewsArticle,
    OptionChain,
    OptionContract,
    PriceBar,
    Quote,
    ScreenerResult,
    SectorInfo,
    Symbol,
)

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
    "EarningsRecord",
    "FinancialRatios",
    "ForwardEstimates",
    "InsiderTrade",
    "InstitutionalHolder",
    "NewsArticle",
    "OptionChain",
    "OptionContract",
    "ScreenerResult",
    "SectorInfo",
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
]

__version__ = "0.1.0"
