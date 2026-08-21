"""Unit tests for AsyncOneFinanceClient."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from onefinance import AsyncOneFinanceClient, OneFinanceClient
from onefinance.core.models import Quote
from onefinance.providers.base import BaseProvider

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class DummyAsyncProvider(BaseProvider):
    name = "yfinance"

    def is_rate_limited(self, response: object) -> bool:
        return False

    def cooldown_for(self, response: object) -> float:
        return 0.0

    def get_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol,
            timestamp=NOW,
            price=150.0,
            change_pct=0.67,
            volume=5000,
            source="yfinance",
            fetched_at=NOW,
        )

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.get_quote(s) for s in symbols]


@pytest.mark.asyncio
async def test_async_client_context_and_quote(tmp_path: Path) -> None:
    provider = DummyAsyncProvider()
    sync_client = OneFinanceClient(
        providers=[provider],
        fallback_order=["yfinance"],
        cache_dir=tmp_path / "cache",
    )
    async with AsyncOneFinanceClient(sync_client=sync_client) as client:
        quote = await client.get_quote("AAPL")
        assert quote.symbol == "AAPL"
        assert quote.price == 150.0

        batch_quotes = await client.get_quotes(["AAPL"])
        assert len(batch_quotes) == 1

        batch_res = await client.batch(client.sync_client.get_quote, ["AAPL"])
        assert "AAPL" in batch_res
