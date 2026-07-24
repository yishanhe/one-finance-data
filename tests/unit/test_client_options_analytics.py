"""Tests for OneFinanceClient.get_options_analytics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from onefinance.core.client import OneFinanceClient
from onefinance.core.models import OptionChain, OptionContract, OptionsAnalytics


def _contract(
    strike: float,
    volume: int | None,
    open_interest: int | None,
) -> OptionContract:
    return OptionContract(
        contract_symbol=f"TEST{strike}",
        strike=strike,
        volume=volume,
        open_interest=open_interest,
    )


def _chain(calls_vol: int, puts_vol: int, calls_oi: int, puts_oi: int) -> OptionChain:
    now = datetime.now(UTC)
    return OptionChain(
        symbol="AAPL",
        expiration_date=date(2026, 7, 18),
        calls=[_contract(150.0, calls_vol, calls_oi)],
        puts=[_contract(140.0, puts_vol, puts_oi)],
        source="yfinance",
        fetched_at=now,
    )


class TestGetOptionsAnalytics:
    def test_aggregates_pcr_across_expirations(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18), date(2026, 8, 15), date(2026, 9, 19)]
        chains = [
            _chain(calls_vol=100, puts_vol=80, calls_oi=500, puts_oi=400),
            _chain(calls_vol=200, puts_vol=160, calls_oi=1000, puts_oi=800),
            _chain(calls_vol=300, puts_vol=240, calls_oi=1500, puts_oi=1200),
        ]

        with patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations):
            with patch.object(OneFinanceClient, "get_option_chain", side_effect=chains):
                result = client.get_options_analytics("AAPL")

        assert isinstance(result, OptionsAnalytics)
        assert result.total_call_volume == 600
        assert result.total_put_volume == 480
        assert result.pcr_volume is not None
        assert abs(result.pcr_volume - 480 / 600) < 0.001
        assert result.expirations_used == 3

    def test_caps_at_max_expirations(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18) + timedelta(days=30 * i) for i in range(10)]
        chain = _chain(calls_vol=100, puts_vol=80, calls_oi=500, puts_oi=400)

        with patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations):
            with patch.object(
                OneFinanceClient, "get_option_chain", return_value=chain
            ) as mock_chain:
                result = client.get_options_analytics("AAPL", max_expirations=3)

        assert mock_chain.call_count == 3
        assert result.expirations_used == 3

    def test_pcr_none_when_no_call_volume(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18)]
        chain = _chain(calls_vol=0, puts_vol=50, calls_oi=0, puts_oi=100)

        with patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations):
            with patch.object(OneFinanceClient, "get_option_chain", return_value=chain):
                result = client.get_options_analytics("AAPL")

        assert result.pcr_volume is None
        assert result.pcr_oi is None

    def test_none_volumes_treated_as_zero(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18)]
        now = datetime.now(UTC)
        chain = OptionChain(
            symbol="AAPL",
            expiration_date=date(2026, 7, 18),
            calls=[_contract(150.0, None, None)],
            puts=[_contract(140.0, None, None)],
            source="yfinance",
            fetched_at=now,
        )

        with patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations):
            with patch.object(OneFinanceClient, "get_option_chain", return_value=chain):
                result = client.get_options_analytics("AAPL")

        assert result.total_call_volume == 0
        assert result.total_put_volume == 0

    def test_unreliable_oi_flags_and_nulls_pcr_oi(self) -> None:
        """MU 2026-07-08 signature: huge volume, single-digit OI → warn, pcr_oi None."""
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18)]
        chain = _chain(calls_vol=210_000, puts_vol=185_000, calls_oi=3, puts_oi=5)

        with patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations):
            with patch.object(OneFinanceClient, "get_option_chain", return_value=chain):
                result = client.get_options_analytics("MU")

        assert result.oi_reliable is False
        assert result.oi_warning is not None and "implausibly low" in result.oi_warning
        assert result.pcr_oi is None  # not a normal-looking ratio from garbage OI
        assert result.pcr_volume is not None  # volume metrics stay usable
        assert result.total_call_oi == 3  # raw totals still reported
        assert result.total_put_oi == 5

    def test_reliable_oi_keeps_pcr_oi_and_no_warning(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18)]
        chain = _chain(calls_vol=5_000, puts_vol=4_000, calls_oi=8_000, puts_oi=6_000)

        with patch.object(OneFinanceClient, "get_options_expirations", return_value=expirations):
            with patch.object(OneFinanceClient, "get_option_chain", return_value=chain):
                result = client.get_options_analytics("AAPL")

        assert result.oi_reliable is True
        assert result.oi_warning is None
        assert result.pcr_oi is not None

    def test_symbol_uppercased(self) -> None:
        client = OneFinanceClient.__new__(OneFinanceClient)
        expirations = [date(2026, 7, 18)]
        chain = _chain(calls_vol=100, puts_vol=80, calls_oi=500, puts_oi=400)

        with patch.object(
            OneFinanceClient, "get_options_expirations", return_value=expirations
        ) as mock_exp:
            with patch.object(OneFinanceClient, "get_option_chain", return_value=chain):
                client.get_options_analytics("aapl")

        assert mock_exp.call_args[0][0] == "AAPL"
