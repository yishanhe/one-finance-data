"""Smoke test run against a freshly built wheel / sdist before publishing.

Invoked from the publish workflow as:
    uv run --isolated --no-project --with dist/*.whl tests/smoke_test.py
    uv run --isolated --no-project --with dist/*.tar.gz tests/smoke_test.py

Verifies that the installed distribution exposes the public surface and that
the `ofclient` console script is wired correctly.
"""

from __future__ import annotations

import subprocess
import sys

from onefinance import (
    AllProvidersFailedError,
    FinanceError,
    NotSupportedError,
    OneFinanceClient,
    PriceBar,
    Quote,
    __version__,
)

assert isinstance(__version__, str) and __version__, "missing __version__"
assert issubclass(NotSupportedError, FinanceError)
assert issubclass(AllProvidersFailedError, FinanceError)
assert callable(OneFinanceClient)
assert PriceBar.__name__ == "PriceBar"
assert Quote.__name__ == "Quote"

result = subprocess.run(
    ["ofclient", "--help"],
    capture_output=True,
    text=True,
    check=True,
)
combined = (result.stdout + result.stderr).lower()
assert "usage" in combined, f"ofclient --help missing 'Usage' line:\n{combined}"

print(f"smoke test passed: onefinance {__version__}", file=sys.stderr)
