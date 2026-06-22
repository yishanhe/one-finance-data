"""Probe Massive's options endpoints with a real key to validate plan access + JSON shape.

Run:  MASSIVE_API_KEY=... uv run python scripts/probe_massive_options.py AAPL

Answers two questions the unit tests can't (they use canned payloads):
  1. Does this Massive plan expose options data, or does it 403?
     A 403 here is expected on plans without an Options subscription and is
     handled gracefully (NotSupportedError → negative cache, no cooldown).
  2. Are the parsed fields (strike, bid/ask, OI, IV) actually populated?
"""

from __future__ import annotations

import sys

from onefinance.core.errors import NotSupportedError, ProviderError
from onefinance.providers.massive import MassiveProvider


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    provider = MassiveProvider()

    try:
        expirations = provider.get_options_expirations(symbol)
    except NotSupportedError:
        print(f"options_expirations: NOT SUPPORTED on this plan (403) for {symbol}")
        return
    except ProviderError as exc:
        print(f"options_expirations: provider error: {exc.message}")
        return

    print(f"options_expirations: {len(expirations)} dates; first few: {expirations[:5]}")
    if not expirations:
        print("no expirations returned; cannot probe chain")
        return

    exp = expirations[0]
    try:
        chain = provider.get_option_chain(symbol, exp)
    except NotSupportedError:
        print(f"option_chain: NOT SUPPORTED on this plan (403) for {symbol} {exp}")
        return
    except ProviderError as exc:
        print(f"option_chain: provider error: {exc.message}")
        return

    print(f"option_chain {symbol} {exp}: {len(chain.calls)} calls, {len(chain.puts)} puts")
    if chain.calls:
        c = chain.calls[0]
        print(
            f"  sample call: strike={c.strike} bid={c.bid} ask={c.ask} "
            f"last={c.last_price} vol={c.volume} oi={c.open_interest} iv={c.implied_volatility}"
        )


if __name__ == "__main__":
    main()
