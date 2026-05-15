"""YFinance provider adapter.

Wraps the ``yfinance`` library to provide ``get_price_history`` and
``get_info`` endpoints.  yfinance is an unofficial Yahoo Finance
scraper — it's free and unlimited but can break without notice,
so it's always the last-resort tier.

M1 scope: price_history + info only.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import yfinance as yf

from onefinance.core.errors import ProviderError
from onefinance.core.models import (
    AnalystData,
    CompanyInfo,
    CorporateAction,
    InstitutionalHolder,
    NewsArticle,
    OptionChain,
    OptionContract,
    PriceBar,
    ScreenerResult,
    SectorInfo,
)
from onefinance.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_SOURCE = "yfinance"


class YFinanceProvider(BaseProvider):
    """Provider adapter for yfinance (unofficial Yahoo Finance scraper)."""

    name = _SOURCE

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout

    # -------------------------------------------------------------------
    # get_price_history — Type A (historical)
    # -------------------------------------------------------------------

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[PriceBar]:
        """Fetch OHLCV bars via yfinance.

        Parameters
        ----------
        symbol : str
            Ticker symbol (e.g. ``"AAPL"``).
        start, end : date
            Inclusive date range.
        interval : str
            Bar interval — ``"1d"``, ``"1wk"``, ``"1mo"`` etc.
        """
        now = datetime.now(timezone.utc)
        ticker = yf.Ticker(symbol)

        try:
            df = ticker.history(
                start=start.isoformat(),
                end=end.isoformat(),
                interval=interval,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if df is None or df.empty:
            return []

        bars: list[PriceBar] = []
        for idx, row in df.iterrows():
            try:
                # idx is a pandas Timestamp. If tz-aware, it has intraday time.
                bar_date = idx.date() if hasattr(idx, "date") else idx
                bar_ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else None
                bars.append(
                    PriceBar(
                        symbol=symbol.upper(),
                        date=bar_date,
                        timestamp=bar_ts,
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        # yfinance may not have Adj Close in newer versions;
                        # fall back to Close.
                        adj_close=float(
                            row.get("Adj Close", row.get("Close", row["Close"]))
                        ),
                        volume=int(row["Volume"]),
                        source=_SOURCE,
                        fetched_at=now,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Skipping bar for %s on %s: %s",
                    symbol,
                    idx,
                    exc,
                )
                continue

        return bars

    # -------------------------------------------------------------------
    # get_info — Type A (slow-changing)
    # -------------------------------------------------------------------

    def get_info(self, symbol: str) -> CompanyInfo:
        """Fetch company profile via yfinance's ``.info`` dict."""
        now = datetime.now(timezone.utc)
        ticker = yf.Ticker(symbol)

        try:
            info: dict[str, Any] = ticker.info or {}
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance .info failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if not info or info.get("quoteType") is None:
            raise ProviderError(
                code="SYMBOL_NOT_FOUND",
                message=f"No info found for symbol '{symbol}' via yfinance",
                provider=self.name,
                retry_safe=False,
            )

        # Normalise currency — yfinance returns e.g. "USD", sometimes None
        raw_currency = info.get("currency")
        currency: str | None = None
        if raw_currency and isinstance(raw_currency, str) and len(raw_currency) == 3:
            currency = raw_currency.upper()

        return CompanyInfo(
            symbol=symbol.upper(),
            name=info.get("longName") or info.get("shortName") or symbol,
            exchange=info.get("exchange"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            country=info.get("country"),
            market_cap=_safe_float(info.get("marketCap")),
            description=info.get("longBusinessSummary"),
            website=info.get("website"),
            employees=_safe_int(info.get("fullTimeEmployees")),
            currency=currency,
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # Alternative Data Endpoints
    # -------------------------------------------------------------------

    def get_news(self, symbol: str, limit: int = 20) -> list[NewsArticle]:
        """Fetch news from yfinance."""
        from onefinance.core.models import NewsArticle
        now = datetime.now(timezone.utc)
        ticker = yf.Ticker(symbol)
        
        try:
            raw_news = ticker.news or []
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance news failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        articles = []
        for n in raw_news[:limit]:
            try:
                published_at = datetime.fromtimestamp(n.get("providerPublishTime", 0), timezone.utc)
                articles.append(NewsArticle(
                    symbol=symbol.upper(),
                    title=n.get("title", ""),
                    publisher=n.get("publisher", ""),
                    link=n.get("link", ""),
                    published_at=published_at,
                    summary=n.get("summary") or n.get("relatedTickers"),
                    source=_SOURCE,
                    fetched_at=now,
                ))
            except Exception as exc:
                logger.warning("Failed to parse news for %s: %s", symbol, exc)
                continue
        return articles

    def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Fetch dividends and splits from yfinance."""
        from onefinance.core.models import CorporateAction
        now = datetime.now(timezone.utc)
        ticker = yf.Ticker(symbol)
        
        try:
            divs = ticker.dividends
            splits = ticker.splits
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance corporate actions failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        actions = []
        if divs is not None and not divs.empty:
            for dt, val in divs.items():
                date_val = dt.date() if hasattr(dt, "date") else dt
                actions.append(CorporateAction(
                    symbol=symbol.upper(),
                    date=date_val,
                    action_type="dividend",
                    amount=float(val),
                    source=_SOURCE,
                    fetched_at=now,
                ))
        
        if splits is not None and not splits.empty:
            for dt, val in splits.items():
                date_val = dt.date() if hasattr(dt, "date") else dt
                actions.append(CorporateAction(
                    symbol=symbol.upper(),
                    date=date_val,
                    action_type="split",
                    split_ratio=float(val),
                    source=_SOURCE,
                    fetched_at=now,
                ))
        
        # Sort by date descending
        return sorted(actions, key=lambda a: a.date, reverse=True)

    def get_institutional_holders(self, symbol: str) -> list[InstitutionalHolder]:
        """Fetch institutional holders from yfinance."""
        from onefinance.core.models import InstitutionalHolder
        now = datetime.now(timezone.utc)
        ticker = yf.Ticker(symbol)
        
        try:
            df = ticker.institutional_holders
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance institutional holders failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        if df is None or df.empty:
            return []

        holders = []
        for _, row in df.iterrows():
            try:
                holders.append(InstitutionalHolder(
                    symbol=symbol.upper(),
                    holder_name=str(row.get("Holder", "")),
                    shares=int(row.get("Shares", 0)),
                    value=float(row.get("Value", 0)),
                    change=int(row.get("Date Reported", 0)) if "Date Reported" in row else None, # yfinance often lacks exact changes here
                    change_pct=float(row.get("% Out", 0)) if "% Out" in row else None,
                    source=_SOURCE,
                    fetched_at=now,
                ))
            except Exception as exc:
                logger.warning("Failed to parse holder for %s: %s", symbol, exc)
                continue
                
        return holders

    def get_analyst_data(self, symbol: str) -> AnalystData:
        """Fetch analyst ratings from yfinance info."""
        from onefinance.core.models import AnalystData
        now = datetime.now(timezone.utc)
        ticker = yf.Ticker(symbol)
        
        try:
            info = ticker.info or {}
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance analyst info failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        return AnalystData(
            symbol=symbol.upper(),
            target_high=_safe_float(info.get("targetHighPrice")),
            target_low=_safe_float(info.get("targetLowPrice")),
            target_mean=_safe_float(info.get("targetMeanPrice")),
            target_median=_safe_float(info.get("targetMedianPrice")),
            rating_buy=_safe_int(info.get("numberOfAnalystOpinions")), # yfinance doesn't break out strong buy etc reliably in .info
            source=_SOURCE,
            fetched_at=now,
        )

    def get_options_expirations(self, symbol: str) -> list[date]:
        """Fetch available option expiration dates from yfinance."""
        ticker = yf.Ticker(symbol)
        try:
            dates = ticker.options
            return [date.fromisoformat(d) for d in dates]
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance options failed for {symbol}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

    def get_option_chain(self, symbol: str, expiration: date) -> OptionChain:
        """Fetch the option chain for a specific expiration date."""
        now = datetime.now(timezone.utc)
        ticker = yf.Ticker(symbol)
        date_str = expiration.isoformat()

        try:
            chain = ticker.option_chain(date_str)
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance option chain failed for {symbol} on {date_str}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        def _parse_contract(row: Any) -> OptionContract:
            return OptionContract(
                contract_symbol=str(row.get("contractSymbol", "")),
                strike=float(row.get("strike", 0.0)),
                last_price=_safe_float(row.get("lastPrice")),
                bid=_safe_float(row.get("bid")),
                ask=_safe_float(row.get("ask")),
                volume=_safe_int(row.get("volume")),
                open_interest=_safe_int(row.get("openInterest")),
                implied_volatility=_safe_float(row.get("impliedVolatility")),
                in_the_money=bool(row.get("inTheMoney")) if "inTheMoney" in row else None,
            )

        calls = [_parse_contract(row) for _, row in chain.calls.iterrows()] if not chain.calls.empty else []
        puts = [_parse_contract(row) for _, row in chain.puts.iterrows()] if not chain.puts.empty else []

        return OptionChain(
            symbol=symbol.upper(),
            expiration_date=expiration,
            calls=calls,
            puts=puts,
            source=_SOURCE,
            fetched_at=now,
        )

    def get_sector_overview(self, sector: str) -> SectorInfo:
        """Fetch sector overview using yf.Sector."""
        now = datetime.now(timezone.utc)
        
        try:
            sec = yf.Sector(sector.lower())
            overview = sec.overview or {}
            top_df = sec.top_companies
        except Exception as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"yfinance sector failed for {sector}: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        top_companies = []
        if top_df is not None and not top_df.empty:
            top_companies = top_df.index.tolist()[:20]

        return SectorInfo(
            name=sector.title(),
            market_weight=_safe_float(overview.get("market_weight")),
            ytd_return=None,  # Not provided in overview directly
            top_companies=top_companies,
            source=_SOURCE,
            fetched_at=now,
        )

    # -------------------------------------------------------------------
    # Rate-limit detection
    # -------------------------------------------------------------------

    def is_rate_limited(self, response: Any) -> bool:
        """yfinance signals rate limits via empty responses or exceptions.

        Since yfinance is an unofficial scraper, rate limits manifest as
        empty DataFrames, HTTP 429 buried in exceptions, or
        ``YFRateLimitError`` (added in newer yfinance versions).
        """
        if response is None:
            return True
        # Check for yfinance-specific rate limit errors
        if isinstance(response, Exception):
            err_str = str(response).lower()
            return "rate" in err_str or "429" in err_str or "too many" in err_str
        return False

    def cooldown_for(self, response: Any) -> float:
        """yfinance cooldown: 5 minutes (per design doc §7)."""
        return 300.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any) -> float | None:
    """Convert to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _safe_int(value: Any) -> int | None:
    """Convert to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
