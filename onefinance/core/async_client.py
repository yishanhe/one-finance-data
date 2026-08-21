"""AsyncOneFinanceClient — async wrapper around OneFinanceClient."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from onefinance.core.client import OneFinanceClient
from onefinance.core.config import OneFinanceConfig
from onefinance.core.errors import FinanceError
from onefinance.core.models import (
    AnalystData,
    BalanceSheet,
    CashFlow,
    CompanyInfo,
    CorporateAction,
    DCFValuation,
    EarningsCalendarEntry,
    EarningsRecord,
    EconomicEvent,
    FinancialRatios,
    ForwardEstimates,
    IncomeStatement,
    InsiderTrade,
    InstitutionalHolder,
    MarketSentiment,
    NewsArticle,
    OptionChain,
    OptionsAnalytics,
    PeerCompany,
    PriceBar,
    Quote,
    ScreenerResult,
    SectorInfo,
    ShortInterest,
    TreasuryRate,
)
from onefinance.providers.base import BaseProvider

if TYPE_CHECKING:
    from onefinance.indicators.core import TechnicalIndicators
    from onefinance.options.core import GEXSnapshot, IVRankResult, MaxPainResult

R = TypeVar("R")


class AsyncOneFinanceClient:
    """Async wrapper around ``OneFinanceClient`` using ``asyncio.to_thread``.

    Provides async context management and ``async def`` methods for all endpoint
    operations, making it easy to integrate into async web servers and agents.
    """

    def __init__(
        self,
        providers: list[BaseProvider] | BaseProvider | None = None,
        *,
        config: str | Path | OneFinanceConfig | None = None,
        cache_dir: str | Path | None = None,
        cache_size_limit_gb: float | None = None,
        audit: bool = True,
        audit_log_path: str | Path | None = None,
        audit_retention_days: int = 30,
        fallback_order: list[str] | None = None,
        sync_client: OneFinanceClient | None = None,
    ) -> None:
        if sync_client is not None:
            self._client = sync_client
        else:
            self._client = OneFinanceClient(
                providers=providers,
                config=config,
                cache_dir=cache_dir,
                cache_size_limit_gb=cache_size_limit_gb,
                audit=audit,
                audit_log_path=audit_log_path,
                audit_retention_days=audit_retention_days,
                fallback_order=fallback_order,
            )

    @property
    def sync_client(self) -> OneFinanceClient:
        """Access the underlying synchronous ``OneFinanceClient``."""
        return self._client

    async def __aenter__(self) -> AsyncOneFinanceClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close underlying client resources."""
        await asyncio.to_thread(self._client.close)

    # -------------------------------------------------------------------
    # Endpoints
    # -------------------------------------------------------------------

    async def get_price_history(
        self,
        symbol: str,
        start: date | str,
        end: date | str | None = None,
        interval: str = "1d",
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[PriceBar]:
        return await asyncio.to_thread(
            lambda: self._client.get_price_history(
                symbol,
                start,
                end,
                interval,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_quote(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> Quote:
        return await asyncio.to_thread(
            lambda: self._client.get_quote(
                symbol,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_quotes(
        self,
        symbols: list[str],
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[Quote | FinanceError]:
        return await asyncio.to_thread(
            lambda: self._client.get_quotes(
                symbols,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_info(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> CompanyInfo:
        return await asyncio.to_thread(
            lambda: self._client.get_info(
                symbol,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_infos(
        self,
        symbols: list[str],
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[CompanyInfo | FinanceError]:
        return await asyncio.to_thread(
            lambda: self._client.get_infos(
                symbols,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_financials(
        self,
        symbol: str,
        statement: str = "income",
        period: str = "annual",
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[IncomeStatement | BalanceSheet | CashFlow]:
        return await asyncio.to_thread(
            lambda: self._client.get_financials(
                symbol,
                statement,
                period,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_ratios(
        self,
        symbol: str,
        period: str = "annual",
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
        fresh: bool = False,
    ) -> list[FinancialRatios]:
        return await asyncio.to_thread(
            lambda: self._client.get_ratios(
                symbol,
                period,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
                fresh=fresh,
            )
        )

    async def get_earnings(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
        fresh: bool = False,
    ) -> list[EarningsRecord]:
        return await asyncio.to_thread(
            lambda: self._client.get_earnings(
                symbol,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
                fresh=fresh,
            )
        )

    async def get_insider_trades(
        self,
        symbol: str,
        since: date | str | None = None,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[InsiderTrade]:
        return await asyncio.to_thread(
            lambda: self._client.get_insider_trades(
                symbol,
                since=since,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_dcf(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> DCFValuation:
        return await asyncio.to_thread(
            lambda: self._client.get_dcf(
                symbol,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_news(
        self,
        symbol: str,
        limit: int = 10,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[NewsArticle]:
        return await asyncio.to_thread(
            lambda: self._client.get_news(
                symbol,
                limit=limit,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_corporate_actions(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[CorporateAction]:
        return await asyncio.to_thread(
            lambda: self._client.get_corporate_actions(
                symbol,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_institutional_holders(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[InstitutionalHolder]:
        return await asyncio.to_thread(
            lambda: self._client.get_institutional_holders(
                symbol,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_analyst_data(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> AnalystData:
        return await asyncio.to_thread(
            lambda: self._client.get_analyst_data(
                symbol,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_peers(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[PeerCompany]:
        return await asyncio.to_thread(
            lambda: self._client.get_peers(
                symbol,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_options_expirations(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[date]:
        return await asyncio.to_thread(
            lambda: self._client.get_options_expirations(
                symbol,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_option_chain(
        self,
        symbol: str,
        expiration: date,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> OptionChain:
        return await asyncio.to_thread(
            lambda: self._client.get_option_chain(
                symbol,
                expiration,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_options_analytics(
        self,
        symbol: str,
        *,
        max_expirations: int = 4,
        no_cache: bool = False,
        provider: str | None = None,
    ) -> OptionsAnalytics:
        return await asyncio.to_thread(
            lambda: self._client.get_options_analytics(
                symbol,
                max_expirations=max_expirations,
                no_cache=no_cache,
                provider=provider,
            )
        )

    async def get_gex(
        self,
        symbol: str,
        *,
        max_expirations: int = 4,
        no_cache: bool = False,
        provider: str | None = None,
    ) -> GEXSnapshot:
        return await asyncio.to_thread(
            lambda: self._client.get_gex(
                symbol,
                max_expirations=max_expirations,
                no_cache=no_cache,
                provider=provider,
            )
        )

    async def get_max_pain(
        self,
        symbol: str,
        expiration: date,
        *,
        no_cache: bool = False,
        provider: str | None = None,
    ) -> MaxPainResult:
        return await asyncio.to_thread(
            lambda: self._client.get_max_pain(
                symbol,
                expiration,
                no_cache=no_cache,
                provider=provider,
            )
        )

    async def get_iv_rank(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
    ) -> IVRankResult:
        return await asyncio.to_thread(
            lambda: self._client.get_iv_rank(
                symbol,
                no_cache=no_cache,
                provider=provider,
            )
        )

    async def get_sector_overview(
        self,
        sector: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> SectorInfo:
        return await asyncio.to_thread(
            lambda: self._client.get_sector_overview(
                sector,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_earnings_calendar(
        self,
        symbol: str | None = None,
        start: date | str | None = None,
        end: date | str | None = None,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[EarningsCalendarEntry]:
        return await asyncio.to_thread(
            lambda: self._client.get_earnings_calendar(
                symbol=symbol,
                start=start,
                end=end,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_economic_calendar(
        self,
        start: date | str | None = None,
        end: date | str | None = None,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[EconomicEvent]:
        return await asyncio.to_thread(
            lambda: self._client.get_economic_calendar(
                start=start,
                end=end,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_treasury_rates(
        self,
        start: date | str | None = None,
        end: date | str | None = None,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[TreasuryRate]:
        return await asyncio.to_thread(
            lambda: self._client.get_treasury_rates(
                start=start,
                end=end,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_forward_estimates(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[ForwardEstimates]:
        return await asyncio.to_thread(
            lambda: self._client.get_forward_estimates(
                symbol,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_short_interest(
        self,
        symbol: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> ShortInterest:
        return await asyncio.to_thread(
            lambda: self._client.get_short_interest(
                symbol,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_market_sentiment(
        self,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> MarketSentiment:
        return await asyncio.to_thread(
            lambda: self._client.get_market_sentiment(
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def screen_stocks(
        self,
        query: str,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        ttl: int | None = None,
    ) -> list[ScreenerResult]:
        return await asyncio.to_thread(
            lambda: self._client.screen_stocks(
                query,
                no_cache=no_cache,
                provider=provider,
                ttl=ttl,
            )
        )

    async def get_indicators(
        self,
        symbol: str,
        start: date | str | None = None,
        end: date | str | None = None,
        *,
        no_cache: bool = False,
        provider: str | None = None,
        with_quote: bool = True,
    ) -> TechnicalIndicators:
        return await asyncio.to_thread(
            lambda: self._client.get_indicators(
                symbol,
                start=start,
                end=end,
                no_cache=no_cache,
                provider=provider,
                with_quote=with_quote,
            )
        )

    async def batch(
        self,
        func: Callable[..., R],
        symbols: Sequence[str],
        *args: Any,
        max_workers: int = 8,
        **kwargs: Any,
    ) -> dict[str, R]:
        return await asyncio.to_thread(
            lambda: self._client.batch(
                func,
                symbols,
                *args,
                max_workers=max_workers,
                **kwargs,
            )
        )
