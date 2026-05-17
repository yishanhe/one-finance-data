"""Tests for onefinance.core.router — tier walking with cooldown logic."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from typing import Any

import pytest

from onefinance.core.config import CooldownConfig, OneFinanceConfig
from onefinance.core.errors import (
    AllProvidersFailedError,
    InvalidArgumentError,
    NotSupportedError,
    ProviderError,
    RateLimitError,
)
from onefinance.core.models import PriceBar, Quote
from onefinance.core.router import ProviderRouter, ProviderState
from onefinance.providers.base import BaseProvider

# ---------------------------------------------------------------------------
# Helpers — mock providers
# ---------------------------------------------------------------------------


class MockProvider(BaseProvider):
    """A fake provider for testing."""

    def __init__(self, name: str, *, supports_endpoints: list[str] | None = None):
        self.name = name
        self._supports = set(supports_endpoints or [])
        self._call_count = 0

    def get_price_history(self, symbol, start, end, interval="1d"):
        if "price_history" not in self._supports:
            raise NotSupportedError(self.name, "price_history")
        self._call_count += 1
        return [_make_price_bar(symbol, source=self.name)]

    def get_quote(self, symbol):
        if "quote" not in self._supports:
            raise NotSupportedError(self.name, "quote")
        self._call_count += 1
        return _make_quote(symbol, source=self.name)

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 60.0


class RateLimitingProvider(BaseProvider):
    """A provider that always rate-limits."""

    def __init__(self, name: str, cooldown_s: float = 3600.0):
        self.name = name
        self._cooldown_s = cooldown_s

    def get_price_history(self, symbol, start, end, interval="1d"):
        raise RateLimitError(
            provider=self.name,
            message=f"{self.name} rate limit hit",
            retry_after_seconds=int(self._cooldown_s),
        )

    def get_quote(self, symbol):
        raise RateLimitError(
            provider=self.name,
            message=f"{self.name} rate limit hit",
            retry_after_seconds=int(self._cooldown_s),
        )

    def is_rate_limited(self, response: Any) -> bool:
        return True

    def cooldown_for(self, response: Any) -> float:
        return self._cooldown_s


class FailingProvider(BaseProvider):
    """A provider that always fails with a generic error."""

    def __init__(self, name: str):
        self.name = name

    def get_price_history(self, symbol, start, end, interval="1d"):
        raise ProviderError(
            code="NETWORK_ERROR",
            message=f"{self.name} network error",
            provider=self.name,
            retry_safe=True,
        )

    def get_quote(self, symbol):
        raise ProviderError(
            code="NETWORK_ERROR",
            message=f"{self.name} network error",
            provider=self.name,
            retry_safe=True,
        )

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 60.0


def _make_price_bar(symbol: str, source: str = "test") -> PriceBar:
    return PriceBar(
        symbol=symbol.upper(),
        date=date(2024, 1, 2),
        open=100.0,
        high=105.0,
        low=99.0,
        close=103.0,
        adj_close=103.0,
        volume=1000000,
        source=source,
        fetched_at=datetime.now(UTC),
    )


def _make_quote(symbol: str, source: str = "test") -> Quote:
    return Quote(
        symbol=symbol.upper(),
        timestamp=datetime.now(UTC),
        price=150.0,
        volume=500000,
        source=source,
        fetched_at=datetime.now(UTC),
    )


def _make_config(
    tiers: dict | None = None,
    cooldown_initial: float = 60.0,
    cooldown_max: float = 3600.0,
) -> OneFinanceConfig:
    return OneFinanceConfig(
        tiers=tiers
        or {
            "price_history": ["prov_a", "prov_b", "prov_c"],
            "quote": ["prov_a", "prov_b"],
        },
        cooldown=CooldownConfig(
            default_initial_s=cooldown_initial,
            max_backoff_s=cooldown_max,
        ),
    )


# ---------------------------------------------------------------------------
# ProviderState tests
# ---------------------------------------------------------------------------


class TestProviderState:
    """Tests for the ProviderState dataclass."""

    def test_initial_state_is_available(self):
        state = ProviderState(name="fmp")
        assert state.is_available
        assert state.cooldown_remaining == 0.0
        assert state.consecutive_failures == 0
        assert state.last_error is None

    def test_mark_success_resets_state(self):
        state = ProviderState(name="fmp")
        state.consecutive_failures = 3
        state.last_error = "some error"
        state.cooldown_until = time.time() + 1000

        state.mark_success()

        assert state.is_available
        assert state.consecutive_failures == 0
        assert state.last_error is None

    def test_mark_failure_sets_cooldown(self):
        state = ProviderState(name="fmp")
        state.mark_failure("rate limit", 60.0)

        assert not state.is_available
        assert state.consecutive_failures == 1
        assert state.last_error == "rate limit"
        assert state.cooldown_remaining > 0

    def test_exponential_backoff(self):
        state = ProviderState(name="fmp")

        # First failure: 60s
        state.mark_failure("error", 60.0)
        first_cooldown = state.cooldown_until - time.time()

        # Second failure: 120s (60 * 2^1)
        state.mark_failure("error", 60.0)
        second_cooldown = state.cooldown_until - time.time()

        assert second_cooldown > first_cooldown

    def test_backoff_capped_at_max(self):
        state = ProviderState(name="fmp")

        # Many failures — should cap at max_backoff
        for _ in range(10):
            state.mark_failure("error", 60.0, max_backoff=300.0)

        cooldown = state.cooldown_until - time.time()
        assert cooldown <= 300.0 + 1.0  # small tolerance

    def test_to_dict(self):
        state = ProviderState(name="fmp")
        d = state.to_dict()
        assert d["name"] == "fmp"
        assert d["available"] is True
        assert d["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# ProviderRouter — basic dispatch
# ---------------------------------------------------------------------------


class TestRouterBasicDispatch:
    """Tests for basic provider routing without cooldowns."""

    def test_dispatches_to_first_tier_provider(self):
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        prov_b = MockProvider("prov_b", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_a"
        assert prov_a._call_count == 1
        assert prov_b._call_count == 0

    def test_skips_unsupported_provider(self):
        prov_a = MockProvider("prov_a", supports_endpoints=[])  # no support
        prov_b = MockProvider("prov_b", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_b"

    def test_force_specific_provider(self):
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        prov_b = MockProvider("prov_b", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
            provider_name="prov_b",
        )

        assert result[0].source == "prov_b"

    def test_force_unknown_provider_raises(self):
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a}, config)

        with pytest.raises(InvalidArgumentError, match="not found"):
            router.dispatch(
                "price_history",
                lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
                provider_name="nonexistent",
            )

    def test_all_providers_fail_raises(self):
        prov_a = FailingProvider("prov_a")
        prov_b = FailingProvider("prov_b")
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        with pytest.raises(AllProvidersFailedError) as exc_info:
            router.dispatch(
                "price_history",
                lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
            )

        assert len(exc_info.value.failures) == 2


# ---------------------------------------------------------------------------
# ProviderRouter — cooldown handling
# ---------------------------------------------------------------------------


class TestRouterCooldown:
    """Tests for cooldown/rate-limit handling."""

    def test_rate_limited_provider_goes_to_cooldown(self):
        rate_limited = RateLimitingProvider("prov_a")
        fallback = MockProvider("prov_b", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": rate_limited, "prov_b": fallback}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        # Should fall through to prov_b
        assert result[0].source == "prov_b"

        # prov_a should be in cooldown
        state = router.get_provider_state("prov_a")
        assert state is not None
        assert not state.is_available
        assert state.consecutive_failures == 1

    def test_cooldown_provider_skipped_on_next_call(self):
        rate_limited = RateLimitingProvider("prov_a")
        fallback = MockProvider("prov_b", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": rate_limited, "prov_b": fallback}, config)

        # First call — triggers rate limit on prov_a, falls to prov_b
        router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        # Second call — prov_a should be skipped (in cooldown)
        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_b"
        # prov_a should still not have been tried again
        assert fallback._call_count == 2

    def test_all_rate_limited_includes_cooldown_as_fallback(self):
        prov_a = RateLimitingProvider("prov_a")
        prov_b = RateLimitingProvider("prov_b")
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        # Both fail on first call
        with pytest.raises(AllProvidersFailedError) as exc_info:
            router.dispatch(
                "price_history",
                lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
            )

        # Second call — both in cooldown, error includes them as fallbacks
        with pytest.raises(AllProvidersFailedError) as exc_info:
            router.dispatch(
                "price_history",
                lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
            )

        assert "prov_a" in exc_info.value.fallback_providers_available
        assert "prov_b" in exc_info.value.fallback_providers_available

    def test_forced_provider_bypasses_cooldown(self):
        """When a provider is explicitly forced, skip the cooldown check."""
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a}, config)

        # Manually put prov_a in cooldown
        state = router.get_provider_state("prov_a")
        state.cooldown_until = time.time() + 9999

        # Force it anyway
        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
            provider_name="prov_a",
        )

        assert result[0].source == "prov_a"

    def test_success_resets_cooldown(self):
        """After a successful call, provider's failure state is cleared."""
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a}, config)

        # Manually set some failure state
        state = router.get_provider_state("prov_a")
        state.consecutive_failures = 3
        state.last_error = "old error"

        # Successful call
        router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert state.consecutive_failures == 0
        assert state.last_error is None

    def test_generic_failure_also_triggers_cooldown(self):
        """Non-rate-limit failures also put provider in cooldown."""
        failing = FailingProvider("prov_a")
        fallback = MockProvider("prov_b", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": failing, "prov_b": fallback}, config)

        router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        state = router.get_provider_state("prov_a")
        assert not state.is_available
        assert state.consecutive_failures == 1


# ---------------------------------------------------------------------------
# ProviderRouter — tier list configuration
# ---------------------------------------------------------------------------


class TestRouterTierConfig:
    """Tests for config-driven tier ordering."""

    def test_uses_tier_order_from_config(self):
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        prov_b = MockProvider("prov_b", supports_endpoints=["price_history"])
        # Config puts prov_b first
        config = _make_config(tiers={"price_history": ["prov_b", "prov_a"]})
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_b"

    def test_type_c_uses_fresh_tier_list(self):
        prov_a = MockProvider("prov_a", supports_endpoints=["quote"])
        prov_b = MockProvider("prov_b", supports_endpoints=["quote"])
        config = _make_config(
            tiers={
                "ratios": {
                    "default": ["prov_b", "prov_a"],
                    "fresh": ["prov_a", "prov_b"],
                },
            }
        )
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        # fresh=True → use "fresh" list → prov_a first
        result = router.dispatch(
            "ratios",
            lambda p: _make_price_bar("AAPL", source=p.name),
            fresh=True,
        )
        assert result.source == "prov_a"

    def test_type_c_default_tier_list(self):
        prov_a = MockProvider("prov_a", supports_endpoints=["quote"])
        prov_b = MockProvider("prov_b", supports_endpoints=["quote"])
        config = _make_config(
            tiers={
                "ratios": {
                    "default": ["prov_b", "prov_a"],
                    "fresh": ["prov_a", "prov_b"],
                },
            }
        )
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        # fresh=False → use "default" list → prov_b first
        result = router.dispatch(
            "ratios",
            lambda p: _make_price_bar("AAPL", source=p.name),
            fresh=False,
        )
        assert result.source == "prov_b"

    def test_unknown_endpoint_falls_back_to_all_providers(self):
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        config = _make_config(tiers={})  # no tiers at all
        router = ProviderRouter({"prov_a": prov_a}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_a"

    def test_missing_tier_providers_are_skipped(self):
        """Tier list references providers not in the registry — they're ignored."""
        prov_b = MockProvider("prov_b", supports_endpoints=["price_history"])
        config = _make_config(tiers={"price_history": ["prov_a", "prov_b"]})
        # Only prov_b is registered
        router = ProviderRouter({"prov_b": prov_b}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_b"


# ---------------------------------------------------------------------------
# ProviderRouter — state inspection
# ---------------------------------------------------------------------------


class TestRouterStateInspection:
    """Tests for router state inspection methods."""

    def test_state_returns_all_providers(self):
        prov_a = MockProvider("prov_a", supports_endpoints=[])
        prov_b = MockProvider("prov_b", supports_endpoints=[])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        state = router.state()
        assert "prov_a" in state
        assert "prov_b" in state
        assert state["prov_a"]["available"] is True
        assert state["prov_b"]["available"] is True

    def test_reset_cooldowns(self):
        prov_a = MockProvider("prov_a", supports_endpoints=[])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a}, config)

        # Put in cooldown
        s = router.get_provider_state("prov_a")
        s.mark_failure("error", 60.0)
        assert not s.is_available

        # Reset
        router.reset_cooldowns()
        assert s.is_available
        assert s.consecutive_failures == 0
