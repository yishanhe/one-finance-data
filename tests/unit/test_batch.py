"""Unit tests for OneFinanceClient.batch."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from onefinance.core.client import OneFinanceClient
from onefinance.core.models import Quote
from onefinance.providers.base import BaseProvider

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class DummyBatchProvider(BaseProvider):
    name = "yfinance"

    def is_rate_limited(self, response: object) -> bool:
        return False

    def cooldown_for(self, response: object) -> float:
        return 0.0

    def get_quote(self, symbol: str) -> Quote:
        return Quote(
            symbol=symbol,
            timestamp=NOW,
            price=100.0,
            change_pct=0.0,
            volume=1000,
            source="yfinance",
            fetched_at=NOW,
        )


def test_client_batch_executes_concurrently(tmp_path: Path) -> None:
    provider = DummyBatchProvider()
    client = OneFinanceClient(
        providers=[provider],
        fallback_order=["yfinance"],
        cache_dir=tmp_path / "cache",
    )
    symbols = ["BTCA", "BTCB", "BTCC"]
    results = client.batch(client.get_quote, symbols, max_workers=3)

    assert set(results.keys()) == set(symbols)
    assert results["BTCA"].price == 100.0
    assert results["BTCB"].symbol == "BTCB"
