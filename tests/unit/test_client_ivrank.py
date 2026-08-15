"""Tests for OneFinanceClient.get_iv_rank."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from onefinance.cache.manager import CacheManager
from onefinance.core.client import OneFinanceClient
from onefinance.core.models import OptionChain, OptionContract, Quote
from onefinance.options.core import IVRankResult


def _contract(strike: float, *, implied_volatility: float | None) -> OptionContract:
    return OptionContract(
        contract_symbol=f"TEST{strike}",
        strike=strike,
        implied_volatility=implied_volatility,
    )


def _chain(expiration: date, iv: float) -> OptionChain:
    return OptionChain(
        symbol="AAPL",
        expiration_date=expiration,
        calls=[_contract(200, implied_volatility=iv)],
        puts=[_contract(200, implied_volatility=iv)],
        source="iv_test",
        fetched_at=datetime.now(UTC),
    )


def _quote(price: float = 200.0) -> Quote:
    return Quote(
        symbol="AAPL",
        timestamp=datetime.now(UTC),
        price=price,
        volume=1_000_000,
        source="iv_test",
        fetched_at=datetime.now(UTC),
    )


@pytest.fixture
def client(tmp_path: Path) -> OneFinanceClient:
    c = OneFinanceClient.__new__(OneFinanceClient)
    c._cache = CacheManager(cache_dir=tmp_path / "iv_cache", size_limit_gb=0.01)
    return c


class TestGetIVRank:
    def test_defaults_to_nearest_expiration(self, client: OneFinanceClient) -> None:
        expirations = [date(2026, 8, 21), date(2026, 9, 18)]
        chain = _chain(date(2026, 8, 21), iv=0.35)

        with (
            patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations),
            patch.object(OneFinanceClient, "get_option_chain", return_value=chain) as mock_chain,
            patch.object(OneFinanceClient, "get_quote", return_value=_quote()),
        ):
            result = client.get_iv_rank("aapl")
            mock_chain.assert_called_once_with(
                "AAPL", date(2026, 8, 21), no_cache=False, provider=None
            )

        assert isinstance(result, IVRankResult)
        assert result.symbol == "AAPL"
        assert result.expiration_used == date(2026, 8, 21)
        assert result.atm_iv == pytest.approx(0.35)
        assert result.insufficient_history is True  # only one observation so far

    def test_uses_explicit_expiration(self, client: OneFinanceClient) -> None:
        chain = _chain(date(2026, 12, 18), iv=0.40)

        with (
            patch.object(OneFinanceClient, "get_option_chain", return_value=chain) as mock_chain,
            patch.object(OneFinanceClient, "get_quote", return_value=_quote()),
        ):
            result = client.get_iv_rank("AAPL", expiration=date(2026, 12, 18))
            mock_chain.assert_called_once_with(
                "AAPL", date(2026, 12, 18), no_cache=False, provider=None
            )

        assert result.expiration_used == date(2026, 12, 18)

    def test_records_observation_and_ranks_against_history(self, client: OneFinanceClient) -> None:
        today = date.today()
        for offset, iv in [(5, 0.20), (4, 0.25), (3, 0.40), (2, 0.50)]:
            client._cache.record_iv_observation("AAPL", today - timedelta(days=offset), iv)

        chain = _chain(date(2026, 8, 21), iv=0.35)
        with (
            patch.object(
                OneFinanceClient, "get_options_expirations", return_value=[date(2026, 8, 21)]
            ),
            patch.object(OneFinanceClient, "get_option_chain", return_value=chain),
            patch.object(OneFinanceClient, "get_quote", return_value=_quote()),
        ):
            result = client.get_iv_rank("AAPL")

        assert result.insufficient_history is False
        assert result.history_points == 5  # 4 seeded + today's new reading
        assert result.iv_rank == pytest.approx((0.35 - 0.20) / (0.50 - 0.20) * 100, abs=0.01)

        # Re-querying today does not double-count today's observation.
        history = client._cache.get_iv_history("AAPL", lookback_days=252)
        assert len(history) == 5

    def test_no_expirations_raises_value_error(self, client: OneFinanceClient) -> None:
        with patch.object(OneFinanceClient, "get_options_expirations", return_value=[]):
            with pytest.raises(ValueError, match="No option expirations"):
                client.get_iv_rank("AAPL")

    def test_no_iv_data_raises_value_error(self, client: OneFinanceClient) -> None:
        chain = OptionChain(
            symbol="AAPL",
            expiration_date=date(2026, 8, 21),
            calls=[_contract(200, implied_volatility=None)],
            puts=[_contract(200, implied_volatility=None)],
            source="iv_test",
            fetched_at=datetime.now(UTC),
        )
        with (
            patch.object(
                OneFinanceClient, "get_options_expirations", return_value=[date(2026, 8, 21)]
            ),
            patch.object(OneFinanceClient, "get_option_chain", return_value=chain),
            patch.object(OneFinanceClient, "get_quote", return_value=_quote()),
        ):
            with pytest.raises(ValueError, match="No implied-volatility data"):
                client.get_iv_rank("AAPL")
