"""Unit tests for AsyncOneFinanceClient."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from onefinance import AsyncOneFinanceClient, OneFinanceClient
from onefinance.core.models import Quote
from onefinance.providers.base import BaseProvider

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [name for name in dir(OneFinanceClient) if name.startswith("get_")],
)
async def test_async_endpoint_parity_and_forwarding(name: str) -> None:
    sync_method = getattr(OneFinanceClient, name)
    async_method = getattr(AsyncOneFinanceClient, name)
    parameters = inspect.signature(sync_method).parameters
    assert inspect.signature(async_method).parameters == parameters

    sync_client = Mock(spec=OneFinanceClient)
    client = AsyncOneFinanceClient(sync_client=sync_client)
    values = {key: object() for key in parameters if key != "self"}
    result = await getattr(client, name)(**values)
    called = getattr(sync_client, name)
    called.assert_called_once()
    bound = inspect.signature(sync_method).bind(
        sync_client, *called.call_args.args, **called.call_args.kwargs
    )
    assert {key: value for key, value in bound.arguments.items() if key != "self"} == values
    assert result is called.return_value


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
