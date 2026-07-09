"""Unit tests for BaseProvider ABC and capability discovery."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from onefinance.core.errors import NotSupportedError
from onefinance.core.models import CompanyInfo, PriceBar, Quote
from onefinance.providers.base import BaseProvider


class _MinimalProvider(BaseProvider):
    """Provider that implements nothing — used to test default stubs."""

    name = "minimal"

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 0.0


class _PriceOnlyProvider(BaseProvider):
    """Provider that overrides only get_price_history."""

    name = "price_only"

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[PriceBar]:
        return []

    def get_info(self, symbol: str) -> CompanyInfo:
        raise NotSupportedError(self.name, "info")  # explicitly re-raises

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 0.0


class TestBaseProviderDefaults:
    """Default stubs should raise NotSupportedError."""

    def setup_method(self) -> None:
        self.provider = _MinimalProvider()

    def test_get_price_history_raises(self) -> None:
        with pytest.raises(NotSupportedError) as exc_info:
            self.provider.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31))
        assert exc_info.value.endpoint == "price_history"
        assert exc_info.value.provider == "minimal"

    def test_get_quote_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_quote("AAPL")

    def test_get_quotes_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_quotes(["AAPL", "MSFT"])

    def test_get_financials_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_financials("AAPL", "income", "annual")

    def test_get_info_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_info("AAPL")

    def test_get_infos_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_infos(["AAPL", "MSFT"])

    def test_get_ratios_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_ratios("AAPL", "annual")

    def test_get_earnings_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_earnings("AAPL")

    def test_get_insider_trades_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_insider_trades("AAPL")


class TestCapabilityDiscovery:
    def test_minimal_supports_nothing(self) -> None:
        p = _MinimalProvider()
        assert p.supports("price_history") is False
        assert p.supports("quote") is False
        assert p.supports("quotes") is False
        assert p.supports("info") is False
        assert p.supports("infos") is False
        assert p.supported_endpoints == []

    def test_price_only_supports_price_history(self) -> None:
        p = _PriceOnlyProvider()
        assert p.supports("price_history") is True
        # get_info is overridden (even though it re-raises), so supports() is True
        assert p.supports("info") is True
        assert p.supports("infos") is True
        assert p.supports("quote") is False
        assert p.supports("quotes") is False
        assert p.supports("ratios") is False
        assert "price_history" in p.supported_endpoints
        assert "info" in p.supported_endpoints

    def test_unknown_endpoint_returns_false(self) -> None:
        p = _MinimalProvider()
        assert p.supports("nonexistent_endpoint") is False


class _QuoteOnlyProvider(BaseProvider):
    name = "quote_only"

    def get_quote(self, symbol: str) -> Quote:
        from datetime import UTC, datetime

        return Quote(
            symbol=symbol,
            price=100.0,
            volume=1000,
            source=self.name,
            timestamp=datetime.now(UTC),
            fetched_at=datetime.now(UTC),
        )

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 0.0


class _InfoOnlyProvider(BaseProvider):
    name = "info_only"

    def get_info(self, symbol: str) -> CompanyInfo:
        from datetime import UTC, datetime

        return CompanyInfo(
            symbol=symbol,
            name=f"{symbol} Inc.",
            source=self.name,
            fetched_at=datetime.now(UTC),
        )

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 0.0


class TestBaseProviderQuotesFallback:
    def test_get_quotes_uses_get_quote_concurrently(self) -> None:
        p = _QuoteOnlyProvider()
        # Even though get_quotes isn't explicitly overridden, it is supported
        # implicitly because the default implementation exists and quote is supported

        # Test fallback works and preserves order
        results = p.get_quotes(["AAPL", "MSFT", "GOOG"])
        assert [q.symbol for q in results] == ["AAPL", "MSFT", "GOOG"]

    def test_get_quotes_capability(self) -> None:
        p = _QuoteOnlyProvider()
        assert p.supports("quote") is True
        assert p.supports("quotes") is True


class TestBaseProviderInfosFallback:
    def test_get_infos_uses_get_info_concurrently(self) -> None:
        p = _InfoOnlyProvider()
        results = p.get_infos(["AAPL", "MSFT", "GOOG"])
        assert [info.symbol for info in results] == ["AAPL", "MSFT", "GOOG"]

    def test_get_infos_capability(self) -> None:
        p = _InfoOnlyProvider()
        assert p.supports("info") is True
        assert p.supports("infos") is True


class TestBaseProviderNewEndpoints:
    """New-generation endpoint stubs raise NotSupportedError."""

    def setup_method(self) -> None:
        self.provider = _MinimalProvider()

    def test_get_dcf_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_dcf("AAPL")

    def test_get_news_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_news("AAPL")

    def test_get_corporate_actions_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_corporate_actions("AAPL")

    def test_get_institutional_holders_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_institutional_holders("AAPL")

    def test_get_analyst_data_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_analyst_data("AAPL")

    def test_get_forward_estimates_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_forward_estimates("AAPL")

    def test_get_options_expirations_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_options_expirations("AAPL")

    def test_get_option_chain_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_option_chain("AAPL", date(2024, 1, 19))

    def test_screen_stocks_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.screen_stocks("sector=Technology")

    def test_get_sector_overview_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_sector_overview("technology")

    def test_get_treasury_rates_raises(self) -> None:
        with pytest.raises(NotSupportedError):
            self.provider.get_treasury_rates(date(2026, 7, 1), date(2026, 7, 2))
