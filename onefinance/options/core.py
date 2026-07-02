"""Options-chain-derived analytics: dealer gamma exposure (GEX) and max pain.

Both are computed purely from ``OptionChain`` data already fetched via
``get_option_chain`` — no separate cache entry, mirroring how
``indicators.core.compute_indicators`` derives from ``get_price_history``
bars rather than caching its own result.

GEX needs per-contract gamma, which today only Tradier populates (ORATS
greeks; see providers/tradier.py). Chains from providers without greeks
(yfinance, Massive) simply contribute nothing to the sum — if *none* of the
supplied contracts carry gamma, ``compute_gex`` raises ``ValueError`` rather
than silently returning an all-zero, meaningless snapshot.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from onefinance.core.models import OptionChain

# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class GEXStrike(BaseModel):
    """Net dealer gamma exposure at a single strike, aggregated across expirations."""

    model_config = ConfigDict(frozen=True)

    strike: float
    call_gamma_exposure: float
    put_gamma_exposure: float
    net_gamma_exposure: float


class GEXSnapshot(BaseModel):
    """Aggregate dealer gamma-exposure profile for a symbol.

    Sign convention (standard "dealer GEX" as popularized by SqueezeMetrics
    and widely used in retail options analytics): dealers are assumed net
    long calls and net short puts sold to customers, so
    ``net_gamma_exposure = call_gamma_exposure - put_gamma_exposure`` at each
    strike. Positive total GEX means dealers are net long gamma — they buy
    dips and sell rallies to stay hedged, which dampens realized volatility.
    Negative total GEX means dealers are net short gamma — they sell dips
    and buy rallies, amplifying moves.

    ``gamma_flip`` is an approximation: the strike nearest to spot at which
    *cumulative* net GEX (strikes summed ascending) crosses zero. A precise
    flip point would re-price gamma at hypothetical spot levels with an
    option pricing model; this uses the observed OI/gamma profile as a
    proxy, which is standard practice for a data client (not a pricing
    engine) and is directionally accurate near current spot.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    spot_price: float
    total_gamma_exposure: float
    gamma_flip: float | None
    strikes: list[GEXStrike]
    expirations_used: int
    source: str
    fetched_at: datetime


class PainPoint(BaseModel):
    """Total option-holder pain (dollar loss to holders) at one candidate strike."""

    model_config = ConfigDict(frozen=True)

    strike: float
    total_pain: float


class MaxPainResult(BaseModel):
    """Max-pain analysis for a single expiration.

    Max pain is the strike at which option writers (sellers) have the
    smallest aggregate payout obligation — equivalently, where option
    holders collectively lose the most. Computed as the standard intrinsic-
    value-at-expiry sum: for candidate strike ``K``,
    ``pain(K) = Σ ITM-call (K - strike) * OI + Σ ITM-put (strike - K) * OI``.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    expiration_date: date
    max_pain_strike: float
    pain_by_strike: list[PainPoint]
    total_call_oi: int
    total_put_oi: int
    source: str
    fetched_at: datetime


# ---------------------------------------------------------------------------
# GEX
# ---------------------------------------------------------------------------

_GAMMA_SCALE = 100 * 0.01  # contract multiplier (100 shares) * SqueezeMetrics 1% spot-move scale


def compute_gex(
    chains: list[OptionChain],
    spot_price: float,
    symbol: str,
    *,
    fetched_at: datetime,
    source: str,
) -> GEXSnapshot:
    """Aggregate per-strike dealer gamma exposure across *chains*.

    Parameters
    ----------
    chains:
        Option chains for one or more expirations (same underlying).
    spot_price:
        Current underlying price, used to scale gamma into dollar exposure.
    symbol:
        Underlying ticker, for the output snapshot.
    fetched_at, source:
        Provenance metadata for the output.

    Raises
    ------
    ValueError
        If no contract in any chain carries gamma data (i.e. every chain
        came from a greeks-less provider), making the computation meaningless.
    """
    by_strike: dict[float, list[float]] = defaultdict(lambda: [0.0, 0.0])  # strike -> [call, put]
    saw_gamma = False

    for chain in chains:
        for contract in chain.calls:
            if contract.gamma is None or contract.open_interest is None:
                continue
            saw_gamma = True
            exposure = contract.gamma * contract.open_interest * (spot_price**2) * _GAMMA_SCALE
            by_strike[contract.strike][0] += exposure
        for contract in chain.puts:
            if contract.gamma is None or contract.open_interest is None:
                continue
            saw_gamma = True
            exposure = contract.gamma * contract.open_interest * (spot_price**2) * _GAMMA_SCALE
            by_strike[contract.strike][1] += exposure

    if not saw_gamma:
        raise ValueError(
            "No gamma data available in the supplied option chains — GEX requires a "
            "greeks-capable provider (e.g. Tradier). Chains from yfinance/Massive alone "
            "cannot compute gamma exposure."
        )

    strikes = [
        GEXStrike(
            strike=strike,
            call_gamma_exposure=round(call_gex, 2),
            put_gamma_exposure=round(put_gex, 2),
            net_gamma_exposure=round(call_gex - put_gex, 2),
        )
        for strike, (call_gex, put_gex) in sorted(by_strike.items())
    ]

    total_gex = round(sum(s.net_gamma_exposure for s in strikes), 2)
    gamma_flip = _find_gamma_flip(strikes)

    return GEXSnapshot(
        symbol=symbol.upper(),
        spot_price=spot_price,
        total_gamma_exposure=total_gex,
        gamma_flip=gamma_flip,
        strikes=strikes,
        expirations_used=len(chains),
        source=source,
        fetched_at=fetched_at,
    )


def _find_gamma_flip(strikes: list[GEXStrike]) -> float | None:
    """Strike where cumulative net GEX (ascending) crosses zero, or None if it never does."""
    if len(strikes) < 2:
        return None
    cumulative = 0.0
    prev_strike: float | None = None
    prev_cumulative = 0.0
    for s in strikes:
        cumulative += s.net_gamma_exposure
        if prev_strike is not None and (
            (prev_cumulative < 0 <= cumulative) or (prev_cumulative > 0 >= cumulative)
        ):
            return prev_strike
        prev_strike = s.strike
        prev_cumulative = cumulative
    return None


# ---------------------------------------------------------------------------
# Max pain
# ---------------------------------------------------------------------------


def compute_max_pain(
    chain: OptionChain,
    *,
    fetched_at: datetime,
    source: str,
) -> MaxPainResult:
    """Compute the max-pain strike for a single option chain.

    Raises
    ------
    ValueError
        If the chain has no contracts with both a strike and open interest.
    """
    call_oi = [(c.strike, c.open_interest) for c in chain.calls if c.open_interest is not None]
    put_oi = [(p.strike, p.open_interest) for p in chain.puts if p.open_interest is not None]

    candidate_strikes = sorted({s for s, _ in call_oi} | {s for s, _ in put_oi})
    if not candidate_strikes:
        raise ValueError(
            f"No open-interest data available for {chain.symbol} {chain.expiration_date} "
            "— cannot compute max pain."
        )

    pain_by_strike: list[PainPoint] = []
    best_strike = candidate_strikes[0]
    best_pain = float("inf")
    for k in candidate_strikes:
        pain = sum(max(0.0, k - strike) * oi for strike, oi in call_oi)
        pain += sum(max(0.0, strike - k) * oi for strike, oi in put_oi)
        pain_by_strike.append(PainPoint(strike=k, total_pain=round(pain, 2)))
        if pain < best_pain:
            best_pain = pain
            best_strike = k

    return MaxPainResult(
        symbol=chain.symbol,
        expiration_date=chain.expiration_date,
        max_pain_strike=best_strike,
        pain_by_strike=pain_by_strike,
        total_call_oi=sum(oi for _, oi in call_oi),
        total_put_oi=sum(oi for _, oi in put_oi),
        source=source,
        fetched_at=fetched_at,
    )
