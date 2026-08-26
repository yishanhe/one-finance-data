"""Unit tests for CboeProvider with mocked HTTP responses."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from onefinance.core.errors import NotSupportedError, ProviderError
from onefinance.providers.cboe import CboeProvider


def _client(payload: Any, status_code: int = 200, seen: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        content = {"text": payload} if isinstance(payload, str) else {"json": payload}
        return httpx.Response(status_code, request=request, **content)

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestGetQuote:
    def test_returns_quote_from_data_payload(self) -> None:
        seen_urls: list[str] = []
        provider = CboeProvider(
            http_client=_client(
                {
                    "timestamp": "2026-07-06 07:46:18",
                    "data": {
                        "symbol": "VIX3M",
                        "current_price": 18.42,
                        "prev_day_close": 18.0,
                        "last_trade_time": "2026-07-02T16:15:01",
                    },
                },
                seen=seen_urls,
            )
        )

        quote = provider.get_quote("VIX3M")

        assert quote.symbol == "VIX3M"
        assert quote.price == 18.42
        assert quote.prev_close == 18.0
        assert quote.change_pct == pytest.approx(2.3333)
        assert quote.timestamp.isoformat() == "2026-07-06T07:46:18+00:00"
        assert quote.source == "cboe"
        assert seen_urls == ["https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX3M.json"]

    def test_returns_quote_from_symbol_keyed_payload(self) -> None:
        provider = CboeProvider(
            http_client=_client(
                {
                    "quotes": {
                        "VXN": {
                            "last": 22.5,
                            "previous_close": 22.0,
                            "volume": 0,
                        }
                    }
                }
            )
        )

        quote = provider.get_quote("VXN")

        assert quote.symbol == "VXN"
        assert quote.price == 22.5
        assert quote.volume == 0

    def test_unsupported_symbol_raises_not_supported(self) -> None:
        provider = CboeProvider(http_client=_client({}))

        with pytest.raises(NotSupportedError):
            provider.get_quote("AAPL")

    def test_zero_price_raises_provider_error(self) -> None:
        provider = CboeProvider(http_client=_client({"data": {"symbol": "RVX", "last": 0}}))

        with pytest.raises(ProviderError, match="No valid Cboe quote"):
            provider.get_quote("RVX")

    def test_404_raises_not_supported(self) -> None:
        provider = CboeProvider(http_client=_client({"error": "missing"}, status_code=404))

        with pytest.raises(NotSupportedError) as exc_info:
            provider.get_quote("VXSMH")
        assert exc_info.value.http_status is None


class TestSymbolAliases:
    def test_caret_form_is_accepted(self) -> None:
        seen_urls: list[str] = []
        provider = CboeProvider(
            http_client=_client(
                {"data": {"symbol": "^VIX", "current_price": 16.9}},
                seen=seen_urls,
            )
        )

        quote = provider.get_quote("^VIX")

        assert quote.price == 16.9
        assert seen_urls == ["https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"]


class TestMarketSentiment:
    def test_returns_put_call_ratios_from_daily_statistics(self) -> None:
        seen_urls: list[str] = []
        provider = CboeProvider(
            http_client=_client(
                r'<script>self.__next_f.push([1,"{\"ratios\":['
                r"{\"name\":\"TOTAL PUT/CALL RATIO\",\"value\":\"0.91\"},"
                r"{\"name\":\"INDEX PUT/CALL RATIO\",\"value\":\"1.00\"},"
                r"{\"name\":\"EQUITY PUT/CALL RATIO\",\"value\":\"0.68\"}"
                r']}"])</script>',
                seen=seen_urls,
            )
        )

        result = provider.get_market_sentiment()

        assert result.pcr_total == 0.91
        assert result.pcr_index == 1.0
        assert result.pcr_equity == 0.68
        assert result.source == "cboe"
        assert seen_urls == ["https://www.cboe.com/data/mktstat.aspx"]

    def test_missing_ratios_raise(self) -> None:
        provider = CboeProvider(http_client=_client("<html></html>"))

        with pytest.raises(ProviderError, match="no put/call ratios"):
            provider.get_market_sentiment()


class TestChangeDerivation:
    def test_prev_close_derived_from_price_change(self) -> None:
        """Cboe's prev_day_close echoes the current close after settlement —
        price_change/price_change_percent are authoritative (C6)."""
        provider = CboeProvider(
            http_client=_client(
                {
                    "data": {
                        "symbol": "^VIX",
                        "current_price": 16.9,
                        "price_change": 0.77,
                        "price_change_percent": 4.5562,
                        "prev_day_close": 16.9,  # broken: equals current close
                    }
                }
            )
        )

        quote = provider.get_quote("VIX")

        assert quote.price == 16.9
        assert quote.prev_close == pytest.approx(16.13)
        assert quote.change_pct == pytest.approx(4.5562)

    def test_falls_back_to_close_keys_without_price_change(self) -> None:
        provider = CboeProvider(
            http_client=_client(
                {"data": {"symbol": "VIX3M", "current_price": 18.42, "prev_day_close": 18.0}}
            )
        )

        quote = provider.get_quote("VIX3M")

        assert quote.prev_close == 18.0
        assert quote.change_pct == pytest.approx(2.3333)

    def test_zero_change_day(self) -> None:
        provider = CboeProvider(
            http_client=_client(
                {
                    "data": {
                        "symbol": "RVX",
                        "current_price": 20.0,
                        "price_change": 0.0,
                        "price_change_percent": 0.0,
                    }
                }
            )
        )

        quote = provider.get_quote("RVX")

        assert quote.prev_close == 20.0
        assert quote.change_pct == 0.0
