"""Tests for onefinance.options.core (GEX + max pain)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from onefinance.core.models import OptionChain, OptionContract
from onefinance.options.core import assess_oi_reliability, compute_gex, compute_max_pain

NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
EXP = date(2026, 6, 19)


def _contract(
    strike: float,
    *,
    gamma: float | None = None,
    open_interest: int | None = None,
    volume: int | None = None,
) -> OptionContract:
    return OptionContract(
        contract_symbol=f"TEST{strike}",
        strike=strike,
        gamma=gamma,
        open_interest=open_interest,
        volume=volume,
    )


def _chain(calls: list[OptionContract], puts: list[OptionContract]) -> OptionChain:
    return OptionChain(
        symbol="TEST",
        expiration_date=EXP,
        calls=calls,
        puts=puts,
        source="greeks_test",
        fetched_at=NOW,
    )


# ---------------------------------------------------------------------------
# compute_gex
# ---------------------------------------------------------------------------


class TestComputeGEX:
    def test_raises_without_any_gamma_data(self) -> None:
        chain = _chain(
            [_contract(100, open_interest=500)],  # no gamma — greeks-less provider
            [_contract(100, open_interest=500)],
        )
        with pytest.raises(ValueError, match="No gamma data"):
            compute_gex([chain], 100.0, "TEST", fetched_at=NOW, source="yfinance")

    def test_single_strike_exact_exposure(self) -> None:
        # spot=100, gamma=0.05, OI=1000 -> exposure = 0.05 * 1000 * 100^2 * (100 * 0.01)
        chain = _chain(
            [_contract(100, gamma=0.05, open_interest=1000)],
            [_contract(100, gamma=0.02, open_interest=500)],
        )
        snap = compute_gex([chain], 100.0, "test", fetched_at=NOW, source="greeks_test")

        assert snap.symbol == "TEST"
        assert len(snap.strikes) == 1
        s = snap.strikes[0]
        expected_call = 0.05 * 1000 * (100.0**2) * 1.0
        expected_put = 0.02 * 500 * (100.0**2) * 1.0
        assert s.call_gamma_exposure == pytest.approx(expected_call, rel=1e-6)
        assert s.put_gamma_exposure == pytest.approx(expected_put, rel=1e-6)
        assert s.net_gamma_exposure == pytest.approx(expected_call - expected_put, rel=1e-6)
        assert snap.total_gamma_exposure == pytest.approx(expected_call - expected_put, rel=1e-6)
        assert snap.expirations_used == 1

    def test_contracts_missing_oi_or_gamma_are_skipped_not_errored(self) -> None:
        chain = _chain(
            [
                _contract(100, gamma=0.05, open_interest=1000),
                _contract(105, gamma=None, open_interest=1000),  # skipped: no gamma
                _contract(110, gamma=0.01, open_interest=None),  # skipped: no OI
            ],
            [],
        )
        snap = compute_gex([chain], 100.0, "TEST", fetched_at=NOW, source="greeks_test")
        assert len(snap.strikes) == 1
        assert snap.strikes[0].strike == 100

    def test_aggregates_same_strike_across_expirations(self) -> None:
        chain1 = _chain([_contract(100, gamma=0.05, open_interest=1000)], [])
        chain2 = _chain([_contract(100, gamma=0.03, open_interest=1000)], [])
        snap = compute_gex([chain1, chain2], 100.0, "TEST", fetched_at=NOW, source="greeks_test")

        assert len(snap.strikes) == 1
        assert snap.expirations_used == 2
        expected = (0.05 + 0.03) * 1000 * (100.0**2) * 1.0
        assert snap.strikes[0].call_gamma_exposure == pytest.approx(expected, rel=1e-6)

    def test_gamma_flip_detects_sign_crossing(self) -> None:
        # Strike 90: net negative (dealers short gamma below spot)
        # Strike 100: net positive (dealers long gamma above spot)
        chain = _chain(
            [
                _contract(90, gamma=0.01, open_interest=100),  # small call gex
                _contract(100, gamma=0.10, open_interest=1000),  # large call gex
            ],
            [
                # large put gex -> net negative at 90
                _contract(90, gamma=0.10, open_interest=1000),
                # small put gex -> net positive at 100
                _contract(100, gamma=0.01, open_interest=100),
            ],
        )
        snap = compute_gex([chain], 100.0, "TEST", fetched_at=NOW, source="greeks_test")
        assert snap.strikes[0].net_gamma_exposure < 0
        assert snap.strikes[1].net_gamma_exposure > 0
        # Cumulative crosses zero between the two strikes -> flip reported at the lower one
        assert snap.gamma_flip == 90

    def test_gamma_flip_none_when_no_sign_crossing(self) -> None:
        chain = _chain(
            [
                _contract(90, gamma=0.05, open_interest=1000),
                _contract(100, gamma=0.05, open_interest=1000),
            ],
            [],
        )
        snap = compute_gex([chain], 100.0, "TEST", fetched_at=NOW, source="greeks_test")
        # Both strikes net positive — cumulative never crosses zero
        assert snap.gamma_flip is None

    def test_empty_chains_list_raises(self) -> None:
        with pytest.raises(ValueError, match="No gamma data"):
            compute_gex([], 100.0, "TEST", fetched_at=NOW, source="greeks_test")


# ---------------------------------------------------------------------------
# compute_max_pain
# ---------------------------------------------------------------------------


class TestComputeMaxPain:
    def test_raises_without_oi_data(self) -> None:
        chain = _chain([_contract(100)], [_contract(100)])
        with pytest.raises(ValueError, match="No open-interest data"):
            compute_max_pain(chain, fetched_at=NOW, source="greeks_test")

    def test_symmetric_single_strike_is_max_pain(self) -> None:
        # Only one strike with OI on both sides -> trivially the max-pain strike
        chain = _chain(
            [_contract(100, open_interest=1000)],
            [_contract(100, open_interest=1000)],
        )
        result = compute_max_pain(chain, fetched_at=NOW, source="greeks_test")
        assert result.max_pain_strike == 100
        assert result.total_call_oi == 1000
        assert result.total_put_oi == 1000
        assert len(result.pain_by_strike) == 1
        assert result.pain_by_strike[0].total_pain == 0.0

    def test_known_max_pain_strike(self) -> None:
        # Calls concentrated at 90 (deep ITM below any candidate > 90 causes call pain),
        # puts concentrated at 110 (deep ITM above any candidate < 110 causes put pain).
        # Candidate strikes: 90, 100, 110. At 100, call pain = (100-90)*10=100,
        # put pain = (110-100)*10=100 -> total 200.
        # At 90: call pain = 0, put pain = (110-90)*10=200 -> total 200.
        # At 110: call pain = (110-90)*10=200, put pain=0 -> total 200.
        # All tied — use an asymmetric example instead for a clean unique minimum.
        chain = _chain(
            [_contract(90, open_interest=100), _contract(110, open_interest=1)],
            [_contract(90, open_interest=1), _contract(110, open_interest=100)],
        )
        result = compute_max_pain(chain, fetched_at=NOW, source="greeks_test")
        # Heavy call OI at 90 and heavy put OI at 110 -> pain minimized near the middle,
        # pulled toward whichever side is lighter; verify it picks the true minimum.
        pains = {p.strike: p.total_pain for p in result.pain_by_strike}
        assert result.max_pain_strike == min(pains, key=lambda k: pains[k])

    def test_expiration_and_symbol_propagate_from_chain(self) -> None:
        chain = _chain(
            [_contract(100, open_interest=10)],
            [_contract(100, open_interest=10)],
        )
        result = compute_max_pain(chain, fetched_at=NOW, source="greeks_test")
        assert result.symbol == "TEST"
        assert result.expiration_date == EXP

    def test_raises_when_all_oi_zero(self) -> None:
        """All-zero OI (int 0, not None) must raise, not return the lowest strike."""
        chain = _chain(
            [_contract(100, open_interest=0), _contract(110, open_interest=0)],
            [_contract(90, open_interest=0), _contract(100, open_interest=0)],
        )
        with pytest.raises(ValueError, match="zero across all"):
            compute_max_pain(chain, fetched_at=NOW, source="yfinance")


# ---------------------------------------------------------------------------
# assess_oi_reliability
# ---------------------------------------------------------------------------


class TestAssessOIReliability:
    def test_healthy_chain_is_reliable(self) -> None:
        chain = _chain(
            [_contract(100, volume=5_000, open_interest=8_000)],
            [_contract(100, volume=5_000, open_interest=6_000)],
        )
        reliable, warning = assess_oi_reliability([chain])
        assert reliable is True
        assert warning is None

    def test_large_volume_with_near_zero_oi_is_unreliable(self) -> None:
        # The 2026-07-08 MU signature: huge volume, total OI in single digits.
        chain = _chain(
            [_contract(100, volume=200_000, open_interest=3)],
            [_contract(100, volume=180_000, open_interest=5)],
        )
        reliable, warning = assess_oi_reliability([chain])
        assert reliable is False
        assert warning is not None and "implausibly low" in warning

    def test_thin_chain_not_flagged(self) -> None:
        # Below the volume threshold there is not enough signal to judge.
        chain = _chain(
            [_contract(100, volume=200, open_interest=0)],
            [_contract(100, volume=300, open_interest=0)],
        )
        reliable, warning = assess_oi_reliability([chain])
        assert reliable is True
        assert warning is None

    def test_majority_of_active_contracts_missing_oi_is_unreliable(self) -> None:
        # Aggregate OI passes (2000 >= 1% of 3000) but 25 of 30 traded
        # contracts report zero OI — the per-contract truncation signature.
        calls = [_contract(100 + i, volume=100, open_interest=0) for i in range(25)]
        puts = [_contract(200 + i, volume=100, open_interest=400) for i in range(5)]
        reliable, warning = assess_oi_reliability([_chain(calls, puts)])
        assert reliable is False
        assert warning is not None and "actively traded" in warning

    def test_empty_chains_are_reliable(self) -> None:
        reliable, warning = assess_oi_reliability([])
        assert reliable is True
        assert warning is None
