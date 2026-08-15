"""Tests for OneFinanceClient.get_gex and get_max_pain."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest

from onefinance.core.client import OneFinanceClient
from onefinance.core.models import OptionChain, OptionContract, Quote
from onefinance.options.core import GEXSnapshot, MaxPainResult


def _contract(
    strike: float,
    *,
    gamma: float | None = None,
    open_interest: int | None = None,
    implied_volatility: float | None = None,
) -> OptionContract:
    return OptionContract(
        contract_symbol=f"TEST{strike}",
        strike=strike,
        gamma=gamma,
        open_interest=open_interest,
        implied_volatility=implied_volatility,
    )


def _chain(calls: list[OptionContract], puts: list[OptionContract]) -> OptionChain:
    return OptionChain(
        symbol="AAPL",
        expiration_date=date(2026, 7, 18),
        calls=calls,
        puts=puts,
        source="greeks_test",
        fetched_at=datetime.now(UTC),
    )


def _quote(price: float = 200.0) -> Quote:
    return Quote(
        symbol="AAPL",
        timestamp=datetime.now(UTC),
        price=price,
        volume=1_000_000,
        source="greeks_test",
        fetched_at=datetime.now(UTC),
    )


class TestGetGEX:
    def test_returns_snapshot_from_greek_chains(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18)]
        chain = _chain(
            [_contract(200, gamma=0.05, open_interest=1000)],
            [_contract(200, gamma=0.02, open_interest=500)],
        )

        with (
            patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations),
            patch.object(OneFinanceClient, "get_quote", return_value=_quote(200.0)),
            patch.object(OneFinanceClient, "get_option_chain", return_value=chain),
        ):
            result = client.get_gex("aapl")

        assert isinstance(result, GEXSnapshot)
        assert result.symbol == "AAPL"
        assert result.spot_price == 200.0
        assert result.expirations_used == 1
        assert len(result.strikes) == 1

    def test_caps_at_max_expirations(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18 + i) for i in range(10)]
        chain = _chain([_contract(200, gamma=0.05, open_interest=1000)], [])

        with (
            patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations),
            patch.object(OneFinanceClient, "get_quote", return_value=_quote(200.0)),
            patch.object(OneFinanceClient, "get_option_chain", return_value=chain) as mock_chain,
        ):
            result = client.get_gex("AAPL", max_expirations=3)

        assert mock_chain.call_count == 3
        assert result.expirations_used == 3

    def test_raises_when_no_provider_has_greeks(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18)]
        # yfinance-style chain: no gamma at all
        chain = _chain([_contract(200, open_interest=1000)], [_contract(200, open_interest=500)])

        with (
            patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations),
            patch.object(OneFinanceClient, "get_quote", return_value=_quote(200.0)),
            patch.object(OneFinanceClient, "get_option_chain", return_value=chain),
        ):
            with pytest.raises(ValueError, match="No gamma data"):
                client.get_gex("AAPL")

    def test_yfinance_style_chain_falls_back_to_black_scholes(self) -> None:
        # No gamma anywhere, but every contract carries IV — the yfinance case.
        client = OneFinanceClient.__new__(OneFinanceClient)
        future_exp = date.today() + timedelta(days=30)
        expirations = [future_exp]
        chain = OptionChain(
            symbol="AAPL",
            expiration_date=future_exp,
            calls=[_contract(200, open_interest=1000, implied_volatility=0.30)],
            puts=[_contract(200, open_interest=500, implied_volatility=0.35)],
            source="yfinance",
            fetched_at=datetime.now(UTC),
        )

        with (
            patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations),
            patch.object(OneFinanceClient, "get_quote", return_value=_quote(200.0)),
            patch.object(OneFinanceClient, "get_option_chain", return_value=chain),
        ):
            result = client.get_gex("AAPL")

        assert result.gamma_source == "black_scholes"
        assert len(result.strikes) == 1
        assert result.total_gamma_exposure != 0.0

    def test_no_bs_gamma_flag_requires_real_greeks(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        future_exp = date.today() + timedelta(days=30)
        chain = OptionChain(
            symbol="AAPL",
            expiration_date=future_exp,
            calls=[_contract(200, open_interest=1000, implied_volatility=0.30)],
            puts=[],
            source="yfinance",
            fetched_at=datetime.now(UTC),
        )

        with (
            patch.object(OneFinanceClient, "get_options_expirations", return_value=[future_exp]),
            patch.object(OneFinanceClient, "get_quote", return_value=_quote(200.0)),
            patch.object(OneFinanceClient, "get_option_chain", return_value=chain),
        ):
            with pytest.raises(ValueError, match="No gamma data"):
                client.get_gex("AAPL", allow_black_scholes_gamma=False)

    def test_mixed_gamma_source_when_some_contracts_have_real_gamma(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        future_exp = date.today() + timedelta(days=30)
        chain = OptionChain(
            symbol="AAPL",
            expiration_date=future_exp,
            calls=[_contract(200, gamma=0.05, open_interest=1000)],  # real gamma
            puts=[_contract(200, open_interest=500, implied_volatility=0.35)],  # IV only
            source="massive",
            fetched_at=datetime.now(UTC),
        )

        with (
            patch.object(OneFinanceClient, "get_options_expirations", return_value=[future_exp]),
            patch.object(OneFinanceClient, "get_quote", return_value=_quote(200.0)),
            patch.object(OneFinanceClient, "get_option_chain", return_value=chain),
        ):
            result = client.get_gex("AAPL")

        assert result.gamma_source == "mixed"


class TestGetMaxPain:
    def test_returns_max_pain_result(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        chain = _chain(
            [_contract(190, open_interest=100)],
            [_contract(210, open_interest=100)],
        )

        with patch.object(OneFinanceClient, "get_option_chain", return_value=chain):
            result = client.get_max_pain("aapl", date(2026, 7, 18))

        assert isinstance(result, MaxPainResult)
        assert result.symbol == "AAPL"
        assert result.expiration_date == date(2026, 7, 18)

    def test_propagates_empty_oi_error(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        chain = _chain([_contract(190)], [_contract(210)])

        with patch.object(OneFinanceClient, "get_option_chain", return_value=chain):
            with pytest.raises(ValueError, match="No open-interest data"):
                client.get_max_pain("AAPL", date(2026, 7, 18))
