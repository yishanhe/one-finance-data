"""Integration tests for TradierProvider hitting the real Tradier Sandbox API.

Gated by the ``integration`` marker (excluded from normal CI) — run with:

    TRADIER_TOKEN=... pytest -m integration tests/integration/test_tradier_live.py

Get a free Sandbox token at developer.tradier.com. These tests validate the
*field mapping* against live JSON — the unit tests only exercise fixtures
authored from the integration brief, so this is what confirms Tradier's real
response shape (e.g. ``option_type``, the ``greeks`` sub-object) matches the
parser. SOXX/SMH are the real downstream targets (GEX / SVIX).

Sandbox data is 15-min delayed and greeks refresh hourly, so assert on shape
and that greeks are populated — never on exact live values.
"""

from __future__ import annotations

import os

import pytest

from onefinance.core.models import OptionChain
from onefinance.providers.tradier import TradierProvider

pytestmark = pytest.mark.integration

_HAS_TOKEN = bool(os.environ.get("TRADIER_TOKEN"))


@pytest.fixture
def tradier() -> TradierProvider:
    return TradierProvider()  # defaults to the Sandbox host


@pytest.mark.skipif(not _HAS_TOKEN, reason="TRADIER_TOKEN not set")
def test_expirations_soxx(tradier: TradierProvider) -> None:
    exps = tradier.get_options_expirations("SOXX")
    assert exps, "expected at least one SOXX expiration"
    assert all(hasattr(d, "isoformat") for d in exps)
    assert exps == sorted(exps)


@pytest.mark.skipif(not _HAS_TOKEN, reason="TRADIER_TOKEN not set")
def test_chain_has_greeks(tradier: TradierProvider) -> None:
    exps = tradier.get_options_expirations("SOXX")
    chain = tradier.get_option_chain("SOXX", exps[0])
    assert isinstance(chain, OptionChain)
    assert chain.source == "tradier"
    assert chain.calls or chain.puts, "expected a non-empty chain"

    # Field-mapping check: greeks must actually populate from the live JSON.
    contracts = chain.calls + chain.puts
    assert any(c.gamma is not None for c in contracts), "no gamma populated — check greeks mapping"
    assert any(c.delta is not None for c in contracts), "no delta populated — check greeks mapping"
