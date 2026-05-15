"""Cache key generation and parameter hashing.

Keys are provider-agnostic: the same ``(symbol, date range)`` from
FMP and yfinance share one cache key.  The ``source`` field on the
returned model tells the caller which provider answered.

Key format::

    "{data_type}:{sha256(sorted_params)[:16]}"
    # example: "price_history:a3f9c2e8b1d4f6a0"

See design doc §10 for the full specification.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def make_key(data_type: str, **params: Any) -> str:
    """Build a cache key from a data type and arbitrary parameters.

    Parameters are JSON-serialised with ``sort_keys=True`` before hashing
    so argument order never causes misses.

    Parameters
    ----------
    data_type:
        Endpoint identifier, e.g. ``"price_history"``, ``"quote"``.
    **params:
        The call parameters (symbol, start, end, period, etc.).
        Values are coerced to strings via ``_normalise_value`` so that
        ``date(2024,1,1)`` and ``"2024-01-01"`` produce the same key.

    Returns
    -------
    str
        A deterministic cache key like ``"price_history:a3f9c2e8b1d4f6a0"``.
    """
    normalised = {k: _normalise_value(v) for k, v in params.items() if v is not None}
    param_json = json.dumps(normalised, sort_keys=True, separators=(",", ":"))
    param_hash = hashlib.sha256(param_json.encode()).hexdigest()[:16]
    return f"{data_type}:{param_hash}"


def _normalise_value(value: Any) -> str | int | float | bool | list | None:
    """Coerce a parameter value to a JSON-friendly primitive.

    Ensures that ``date(2024,1,1)`` and ``"2024-01-01"`` hash identically.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    # date / datetime → ISO string
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
