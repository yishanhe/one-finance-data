"""Validate request symbols using the same constraints as response models."""

from pydantic import TypeAdapter, ValidationError

from onefinance.core.errors import InvalidArgumentError
from onefinance.core.models import Symbol

_SYMBOL = TypeAdapter(Symbol)


def normalize_symbol(symbol: str) -> str:
    """Normalize one ticker and reject invalid input before provider dispatch."""
    try:
        return _SYMBOL.validate_python(symbol.strip().upper())
    except ValidationError as exc:
        raise InvalidArgumentError(
            f"Invalid symbol {symbol!r}: expected one ticker of 1–20 characters "
            "(letters, digits, dots, hyphens, and an optional leading ^)."
        ) from exc
