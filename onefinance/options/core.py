"""Options-chain-derived analytics: GEX, max pain, and IV rank.

GEX and max pain are computed purely from ``OptionChain`` data already
fetched via ``get_option_chain`` — no separate cache entry, mirroring how
``indicators.core.compute_indicators`` derives from ``get_price_history``
bars rather than caching its own result. IV rank differs: it needs a
trailing history of ATM IV that no provider exposes directly, so
``OneFinanceClient.get_iv_rank`` accumulates one itself in the cache (see
``CacheManager.record_iv_observation``) and this module just does the math
over whatever has accumulated so far.

GEX needs per-contract gamma. Chains from providers without greeks simply
contribute nothing to the sum — if *none* of the supplied contracts carry
gamma, ``compute_gex`` raises ``ValueError`` rather than silently returning
an all-zero, meaningless snapshot. yfinance chains are the common case:
Yahoo supplies no greeks, but does supply per-contract implied volatility,
so ``synthesize_missing_gamma`` backfills gamma with a textbook
Black-Scholes formula (see ``black_scholes_gamma``) wherever a contract has
IV but no provider-supplied gamma. ``GEXSnapshot.gamma_source`` discloses
whether a result used real greeks, the BS approximation, or a mix.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from onefinance.core.models import OptionChain, OptionContract

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

    ``gamma_source`` discloses where the per-contract gamma came from:
    ``"provider"`` (real greeks, e.g. Massive with an Options subscription),
    ``"black_scholes"`` (every contract's gamma was derived from its
    provider-supplied implied volatility — the yfinance path), or
    ``"mixed"`` (some contracts had real gamma, others were backfilled).
    The BS approximation assumes European exercise and ignores dividends —
    fine for GEX's purpose (relative gamma concentration across strikes),
    but not a pricing-grade greek.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    spot_price: float
    total_gamma_exposure: float
    gamma_flip: float | None
    strikes: list[GEXStrike]
    expirations_used: int
    gamma_source: Literal["provider", "black_scholes", "mixed"] = "provider"
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
    gamma_source: Literal["provider", "black_scholes", "mixed"] = "provider",
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
    gamma_source:
        Passed straight through to ``GEXSnapshot.gamma_source`` — this
        function only aggregates whatever gamma is already on *chains*; the
        caller (typically ``OneFinanceClient.get_gex`` via
        ``synthesize_missing_gamma``) decides whether that gamma is real or
        BS-derived.

    Raises
    ------
    ValueError
        If no contract in any chain carries gamma data (i.e. every chain
        came from a greeks-less provider with no IV to fall back on either),
        making the computation meaningless.
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
            "No gamma data available in the supplied option chains — GEX needs either "
            "provider-supplied greeks or per-contract implied volatility to derive gamma "
            "via Black-Scholes (see synthesize_missing_gamma). Neither was present."
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
        gamma_source=gamma_source,
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
# Black-Scholes gamma fallback (for greeks-less providers, e.g. yfinance)
# ---------------------------------------------------------------------------

# BS gamma is only weakly sensitive to the risk-free rate (moving it by a
# few hundred bps shifts gamma by a fraction of a percent), so a fixed
# short-term-T-bill-ish default is fine rather than requiring a live rate.
_DEFAULT_RISK_FREE_RATE = 0.04

_SQRT_2PI = math.sqrt(2 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def black_scholes_gamma(
    spot_price: float,
    strike: float,
    time_to_expiry_years: float,
    implied_volatility: float,
    risk_free_rate: float = _DEFAULT_RISK_FREE_RATE,
) -> float | None:
    """Textbook Black-Scholes gamma — identical formula for calls and puts.

    ``gamma = phi(d1) / (S * sigma * sqrt(T))`` where ``phi`` is the
    standard normal density and
    ``d1 = (ln(S/K) + (r + sigma^2/2) * T) / (sigma * sqrt(T))``.

    Returns ``None`` for degenerate inputs (non-positive price, strike,
    time, or IV) rather than raising, since callers apply this per-contract
    across a chain and want to just skip contracts it can't handle.
    """
    if spot_price <= 0 or strike <= 0 or time_to_expiry_years <= 0 or implied_volatility <= 0:
        return None
    sqrt_t = math.sqrt(time_to_expiry_years)
    d1 = (
        math.log(spot_price / strike)
        + (risk_free_rate + 0.5 * implied_volatility**2) * time_to_expiry_years
    ) / (implied_volatility * sqrt_t)
    denom = spot_price * implied_volatility * sqrt_t
    return _norm_pdf(d1) / denom if denom > 0 else None


def synthesize_missing_gamma(
    chain: OptionChain,
    spot_price: float,
    *,
    as_of: date,
    risk_free_rate: float = _DEFAULT_RISK_FREE_RATE,
) -> tuple[OptionChain, bool]:
    """Backfill gamma on contracts that have IV but no provider-supplied gamma.

    Returns a copy of *chain* (contracts unchanged unless backfilled) and
    whether anything was actually filled. Contracts that already carry
    gamma, or that lack implied volatility, or whose expiration is on/before
    *as_of*, are left untouched.
    """
    time_to_expiry_years = (chain.expiration_date - as_of).days / 365.25
    filled = False

    def _fill(contracts: list[OptionContract]) -> list[OptionContract]:
        nonlocal filled
        out: list[OptionContract] = []
        for c in contracts:
            if c.gamma is not None or c.implied_volatility is None or time_to_expiry_years <= 0:
                out.append(c)
                continue
            gamma = black_scholes_gamma(
                spot_price, c.strike, time_to_expiry_years, c.implied_volatility, risk_free_rate
            )
            if gamma is None:
                out.append(c)
                continue
            filled = True
            out.append(c.model_copy(update={"gamma": gamma}))
        return out

    new_calls = _fill(chain.calls)
    new_puts = _fill(chain.puts)
    if not filled:
        return chain, False
    return chain.model_copy(update={"calls": new_calls, "puts": new_puts}), True


# ---------------------------------------------------------------------------
# Open-interest reliability
# ---------------------------------------------------------------------------

_OI_MIN_VOLUME = 1_000  # below this a chain is too thin to judge OI plausibility
_OI_VOLUME_RATIO = 0.01  # total OI below 1% of total volume is implausible
_OI_MIN_ACTIVE_CONTRACTS = 20  # per-contract check needs this many traded contracts
_OI_MISSING_FRACTION = 0.5  # >50% of traded contracts without OI → truncation


def assess_oi_reliability(chains: list[OptionChain]) -> tuple[bool, str | None]:
    """Sanity-check open interest across *chains*.

    Yahoo (yfinance) intermittently zeroes open interest — OCC disseminates
    OI once daily pre-market, and after volatile sessions Yahoo can return
    0/NaN for most strikes while volume stays huge. Downstream OI-based
    analytics (PCR-OI, max pain, walls, GEX) silently become garbage, so
    callers should surface this instead of computing on bad data.

    Returns ``(reliable, warning)``: ``(True, None)`` when OI looks
    plausible, else ``(False, <human-readable reason>)``. Two signatures:

    - aggregate: large total volume with near-zero total OI;
    - per-contract: most actively traded contracts report zero OI.
    """
    contracts = [c for ch in chains for c in (*ch.calls, *ch.puts)]
    total_volume = sum(c.volume or 0 for c in contracts)
    total_oi = sum(c.open_interest or 0 for c in contracts)

    if total_volume >= _OI_MIN_VOLUME and total_oi < max(100.0, total_volume * _OI_VOLUME_RATIO):
        return False, (
            f"total option volume is {total_volume:,} but total open interest is "
            f"{total_oi:,} — implausibly low; the provider likely returned truncated "
            "or stale OI"
        )

    active = [c for c in contracts if (c.volume or 0) > 0]
    missing = [c for c in active if not c.open_interest]
    if len(active) >= _OI_MIN_ACTIVE_CONTRACTS and (
        len(missing) / len(active) > _OI_MISSING_FRACTION
    ):
        return False, (
            f"{len(missing)} of {len(active)} actively traded contracts report zero "
            "open interest — likely OI truncation or staleness at the provider"
        )

    return True, None


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
        If the chain has no contracts with both a strike and open interest,
        or if open interest is zero across every contract (all-zero OI would
        make every candidate strike "max pain" — the answer would just be
        the lowest strike, which is meaningless).
    """
    call_oi = [(c.strike, c.open_interest) for c in chain.calls if c.open_interest is not None]
    put_oi = [(p.strike, p.open_interest) for p in chain.puts if p.open_interest is not None]

    candidate_strikes = sorted({s for s, _ in call_oi} | {s for s, _ in put_oi})
    if not candidate_strikes:
        raise ValueError(
            f"No open-interest data available for {chain.symbol} {chain.expiration_date} "
            "— cannot compute max pain."
        )

    if sum(oi for _, oi in call_oi) + sum(oi for _, oi in put_oi) == 0:
        raise ValueError(
            f"Open interest is zero across all {len(call_oi) + len(put_oi)} contracts for "
            f"{chain.symbol} {chain.expiration_date} — OI data is unavailable or unreliable; "
            "cannot compute max pain."
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


# ---------------------------------------------------------------------------
# IV rank
# ---------------------------------------------------------------------------

# Below this many distinct daily observations, min-max/percentile stats are
# not meaningful (a single day can't be ranked against itself).
_IV_RANK_MIN_HISTORY = 5


class IVRankResult(BaseModel):
    """IV rank/percentile for a symbol's at-the-money implied volatility.

    Unlike GEX/max pain, which are pure point-in-time snapshots derived
    from a single option chain, IV rank is inherently historical — it needs
    the trailing distribution of ATM IV to place today's reading in
    context. No provider here exposes a historical IV series directly, so
    ``OneFinanceClient.get_iv_rank`` builds one itself: each call appends
    today's ATM IV observation (deduped by calendar day) to a persistent
    per-symbol cache history, and this result is computed from whatever has
    accumulated so far. ``iv_rank``/``iv_percentile``/``iv_high``/``iv_low``
    stay ``None`` until at least ``_IV_RANK_MIN_HISTORY`` distinct days have
    been observed (``insufficient_history=True``).

    ``iv_rank`` is the standard min-max normalization popularized by
    tastytrade: ``(atm_iv - iv_low) / (iv_high - iv_low) * 100`` over the
    trailing window. ``iv_percentile`` is the fraction of historical
    observations at or below today's reading — a distribution-shape
    counterpart that isn't skewed by a single extreme outlier the way
    min-max rank can be.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    expiration_used: date
    atm_iv: float
    iv_rank: float | None
    iv_percentile: float | None
    iv_high: float | None
    iv_low: float | None
    lookback_days: int
    history_points: int
    insufficient_history: bool
    source: str
    fetched_at: datetime


def compute_atm_iv(chain: OptionChain, spot_price: float) -> float | None:
    """Average call/put implied volatility at the strike nearest *spot_price*.

    Returns ``None`` if no contract in the chain carries implied volatility.
    """
    contracts: list[OptionContract] = [
        c for c in (*chain.calls, *chain.puts) if c.implied_volatility is not None
    ]
    if not contracts:
        return None
    nearest_strike = min((c.strike for c in contracts), key=lambda k: abs(k - spot_price))
    ivs = [
        c.implied_volatility
        for c in contracts
        if c.strike == nearest_strike and c.implied_volatility is not None
    ]
    return sum(ivs) / len(ivs)


def compute_iv_rank(
    *,
    symbol: str,
    expiration: date,
    atm_iv: float,
    history: list[float],
    lookback_days: int,
    source: str,
    fetched_at: datetime,
) -> IVRankResult:
    """Build an ``IVRankResult`` from *atm_iv* and its trailing *history*.

    *history* is the set of daily ATM-IV observations within the lookback
    window; the caller is expected to have already folded today's reading
    into it.
    """
    insufficient = len(history) < _IV_RANK_MIN_HISTORY
    iv_rank: float | None = None
    iv_percentile: float | None = None
    iv_high: float | None = None
    iv_low: float | None = None

    if not insufficient:
        iv_high = round(max(history), 4)
        iv_low = round(min(history), 4)
        iv_rank = (
            round((atm_iv - iv_low) / (iv_high - iv_low) * 100, 2) if iv_high > iv_low else 50.0
        )
        below_or_equal = sum(1 for v in history if v <= atm_iv)
        iv_percentile = round(below_or_equal / len(history) * 100, 2)

    return IVRankResult(
        symbol=symbol.upper(),
        expiration_used=expiration,
        atm_iv=round(atm_iv, 4),
        iv_rank=iv_rank,
        iv_percentile=iv_percentile,
        iv_high=iv_high,
        iv_low=iv_low,
        lookback_days=lookback_days,
        history_points=len(history),
        insufficient_history=insufficient,
        source=source,
        fetched_at=fetched_at,
    )
