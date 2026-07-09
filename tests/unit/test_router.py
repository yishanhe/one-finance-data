"""Tests for onefinance.core.router — tier walking with cooldown logic."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, Never

import pytest

from onefinance.core.config import AugmentConfig, CooldownConfig, OneFinanceConfig
from onefinance.core.errors import (
    AllProvidersFailedError,
    InvalidArgumentError,
    NotSupportedError,
    ProviderError,
    RateLimitError,
)
from onefinance.core.models import PriceBar, Quote
from onefinance.core.router import ProviderRouter, ProviderState, _is_missing, _merge_model
from onefinance.providers.base import BaseProvider

# ---------------------------------------------------------------------------
# Helpers — mock providers
# ---------------------------------------------------------------------------


class MockProvider(BaseProvider):
    """A fake provider for testing."""

    def __init__(self, name: str, *, supports_endpoints: list[str] | None = None) -> None:
        self.name = name
        self._supports = set(supports_endpoints or [])
        self._call_count = 0

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> list[PriceBar]:
        if "price_history" not in self._supports:
            raise NotSupportedError(self.name, "price_history")
        self._call_count += 1
        return [_make_price_bar(symbol, source=self.name)]

    def get_quote(self, symbol: str) -> Quote:
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

    def __init__(self, name: str, cooldown_s: float = 3600.0) -> None:
        self.name = name
        self._cooldown_s = cooldown_s

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> Never:
        raise RateLimitError(
            provider=self.name,
            message=f"{self.name} rate limit hit",
            retry_after_seconds=int(self._cooldown_s),
        )

    def get_quote(self, symbol: str) -> Never:
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

    def __init__(self, name: str) -> None:
        self.name = name

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
    ) -> Never:
        raise ProviderError(
            code="NETWORK_ERROR",
            message=f"{self.name} network error",
            provider=self.name,
            retry_safe=True,
        )

    def get_quote(self, symbol: str) -> Never:
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


class RouterStateCache:
    """Minimal router cache double for cooldown persistence tests."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, object]] = {}

    def get_negative_global(self, provider: str, endpoint: str) -> bool:
        return False

    def get_negative(self, provider: str, endpoint: str, symbol: str | None) -> bool:
        return False

    def set_negative_global(self, provider: str, endpoint: str, ttl: int = 86400) -> None:
        pass

    def set_negative(
        self, provider: str, endpoint: str, symbol: str | None, ttl: int = 86400
    ) -> None:
        pass

    def get_endpoint_ok(self, provider: str, endpoint: str) -> bool:
        return False

    def mark_endpoint_ok(self, provider: str, endpoint: str, ttl: int = 86400) -> None:
        pass

    def get_augment(self, endpoint: str, symbol: str) -> Any | None:
        return None

    def set_augment(self, endpoint: str, symbol: str, value: Any, ttl: int = 300) -> None:
        pass

    def get_router_state(self, provider: str) -> dict[str, object] | None:
        return self.states.get(provider)

    def set_router_state(
        self, provider: str, state: Mapping[str, object], ttl: int = 14400
    ) -> None:
        self.states[provider] = dict(state)


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
    tiers: dict[str, list[str] | dict[str, list[str]]] | None = None,
    cooldown_initial: float = 60.0,
    cooldown_max: float = 3600.0,
    fallback_order: list[str] | None = None,
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
        fallback_order=fallback_order if fallback_order is not None else [],
    )


# ---------------------------------------------------------------------------
# ProviderState tests
# ---------------------------------------------------------------------------


class TestProviderState:
    """Tests for the ProviderState dataclass."""

    def test_initial_state_is_available(self) -> None:
        state = ProviderState(name="fmp")
        assert state.is_available
        assert state.cooldown_remaining == 0.0
        assert state.consecutive_failures == 0
        assert state.last_error is None

    def test_mark_success_resets_state(self) -> None:
        state = ProviderState(name="fmp")
        state.consecutive_failures = 3
        state.last_error = "some error"
        state.cooldown_until = time.time() + 1000

        state.mark_success()

        assert state.is_available
        assert state.consecutive_failures == 0
        assert state.last_error is None

    def test_mark_failure_sets_cooldown(self) -> None:
        state = ProviderState(name="fmp")
        state.mark_failure("rate limit", 60.0)

        assert not state.is_available
        assert state.consecutive_failures == 1
        assert state.last_error == "rate limit"
        assert state.cooldown_remaining > 0

    def test_exponential_backoff(self) -> None:
        state = ProviderState(name="fmp")

        # First failure: 60s
        state.mark_failure("error", 60.0)
        first_cooldown = state.cooldown_until - time.time()

        # Second failure: 120s (60 * 2^1)
        state.mark_failure("error", 60.0)
        second_cooldown = state.cooldown_until - time.time()

        assert second_cooldown > first_cooldown

    def test_backoff_capped_at_max(self) -> None:
        state = ProviderState(name="fmp")

        # Many failures — should cap at max_backoff
        for _ in range(10):
            state.mark_failure("error", 60.0, max_backoff=300.0)

        cooldown = state.cooldown_until - time.time()
        assert cooldown <= 300.0 + 1.0  # small tolerance

    def test_to_dict(self) -> None:
        state = ProviderState(name="fmp")
        d = state.to_dict()
        assert d["name"] == "fmp"
        assert d["available"] is True
        assert d["consecutive_failures"] == 0

    def test_persisted_snapshot_round_trip(self) -> None:
        state = ProviderState(
            name="fmp",
            cooldown_until=time.time() + 1000,
            last_error="HTTP 429",
            consecutive_failures=2,
        )

        restored = ProviderState(name="fmp")
        restored.restore_persisted(state.to_persisted_dict())

        assert restored.cooldown_until == state.cooldown_until
        assert restored.last_error == "HTTP 429"
        assert restored.consecutive_failures == 2


# ---------------------------------------------------------------------------
# ProviderRouter — basic dispatch
# ---------------------------------------------------------------------------


class TestRouterBasicDispatch:
    """Tests for basic provider routing without cooldowns."""

    def test_dispatches_to_first_tier_provider(self) -> None:
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

    def test_skips_unsupported_provider(self) -> None:
        prov_a = MockProvider("prov_a", supports_endpoints=[])  # no support
        prov_b = MockProvider("prov_b", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_b"

    def test_force_specific_provider(self) -> None:
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

    def test_force_unknown_provider_raises(self) -> None:
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a}, config)

        with pytest.raises(InvalidArgumentError, match="not found"):
            router.dispatch(
                "price_history",
                lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
                provider_name="nonexistent",
            )

    def test_all_providers_fail_raises(self) -> None:
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

    def test_rate_limited_provider_goes_to_cooldown(self) -> None:
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

    def test_cooldown_provider_skipped_on_next_call(self) -> None:
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

    def test_all_rate_limited_includes_cooldown_as_fallback(self) -> None:
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

    def test_forced_provider_bypasses_cooldown(self) -> None:
        """When a provider is explicitly forced, skip the cooldown check."""
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a}, config)

        # Manually put prov_a in cooldown
        state = router.get_provider_state("prov_a")
        assert state is not None
        state.cooldown_until = time.time() + 9999

        # Force it anyway
        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
            provider_name="prov_a",
        )

        assert result[0].source == "prov_a"

    def test_success_resets_cooldown(self) -> None:
        """After a successful call, provider's failure state is cleared."""
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a}, config)

        # Manually set some failure state
        state = router.get_provider_state("prov_a")
        assert state is not None
        state.consecutive_failures = 3
        state.last_error = "old error"

        # Successful call
        router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert state.consecutive_failures == 0
        assert state.last_error is None

    def test_generic_failure_also_triggers_cooldown(self) -> None:
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
        assert state is not None
        assert not state.is_available
        assert state.consecutive_failures == 1

    def test_cooldown_state_restores_from_cache(self) -> None:
        cache = RouterStateCache()
        config = _make_config()
        router = ProviderRouter(
            {
                "prov_a": RateLimitingProvider("prov_a"),
                "prov_b": MockProvider("prov_b", supports_endpoints=["price_history"]),
            },
            config,
            cache=cache,
        )

        router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        persisted = cache.states["prov_a"]
        cooldown_until = persisted["cooldown_until"]
        assert isinstance(cooldown_until, float)
        assert cooldown_until > time.time()
        assert persisted["consecutive_failures"] == 1

        restored = ProviderRouter(
            {
                "prov_a": MockProvider("prov_a", supports_endpoints=["price_history"]),
                "prov_b": MockProvider("prov_b", supports_endpoints=["price_history"]),
            },
            config,
            cache=cache,
        )
        state = restored.get_provider_state("prov_a")
        assert state is not None
        assert not state.is_available
        assert state.consecutive_failures == 1


# ---------------------------------------------------------------------------
# ProviderRouter — tier list configuration
# ---------------------------------------------------------------------------


class TestRouterTierConfig:
    """Tests for config-driven tier ordering."""

    def test_uses_tier_order_from_config(self) -> None:
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

    def test_type_c_uses_fresh_tier_list(self) -> None:
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

    def test_type_c_default_tier_list(self) -> None:
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

    def test_unknown_endpoint_falls_back_to_all_providers(self) -> None:
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        config = _make_config(tiers={})  # no tiers at all
        router = ProviderRouter({"prov_a": prov_a}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_a"

    def test_missing_tier_providers_are_skipped(self) -> None:
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

    def test_state_returns_all_providers(self) -> None:
        prov_a = MockProvider("prov_a", supports_endpoints=[])
        prov_b = MockProvider("prov_b", supports_endpoints=[])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        state = router.state()
        assert "prov_a" in state
        assert "prov_b" in state
        assert state["prov_a"]["available"] is True
        assert state["prov_b"]["available"] is True

    def test_reset_cooldowns(self) -> None:
        prov_a = MockProvider("prov_a", supports_endpoints=[])
        config = _make_config()
        router = ProviderRouter({"prov_a": prov_a}, config)

        # Put in cooldown
        s = router.get_provider_state("prov_a")
        assert s is not None
        s.mark_failure("error", 60.0)
        assert not s.is_available

        # Reset
        router.reset_cooldowns()
        assert s.is_available
        assert s.consecutive_failures == 0


# ---------------------------------------------------------------------------
# _is_missing / _merge_model unit tests
# ---------------------------------------------------------------------------


class TestMergeHelpers:
    def test_is_missing_none(self) -> None:
        assert _is_missing(None) is True

    def test_is_missing_zero_int(self) -> None:
        assert _is_missing(0) is True

    def test_is_missing_zero_float(self) -> None:
        assert _is_missing(0.0) is True

    def test_is_missing_positive(self) -> None:
        assert _is_missing(1) is False
        assert _is_missing(0.1) is False

    def test_is_missing_string(self) -> None:
        assert _is_missing("") is False  # empty string is not missing (not numeric)
        assert _is_missing("x") is False

    def test_merge_fills_missing_volume(self) -> None:
        now = datetime.now(UTC)
        base = Quote(
            symbol="AAPL", timestamp=now, price=150.0, volume=0, source="finnhub", fetched_at=now
        )
        filler = Quote(
            symbol="AAPL",
            timestamp=now,
            price=150.5,
            volume=5_000_000,
            source="yfinance",
            fetched_at=now,
        )
        merged = _merge_model(base, filler, ["volume", "bid", "ask"])
        assert merged.volume == 5_000_000
        assert merged.price == 150.0  # primary price preserved
        assert merged.source == "finnhub+yfinance"

    def test_merge_no_change_when_base_complete(self) -> None:
        now = datetime.now(UTC)
        base = Quote(
            symbol="AAPL",
            timestamp=now,
            price=150.0,
            volume=1_000_000,
            bid=149.9,
            ask=150.1,
            source="finnhub",
            fetched_at=now,
        )
        filler = Quote(
            symbol="AAPL",
            timestamp=now,
            price=150.5,
            volume=5_000_000,
            source="yfinance",
            fetched_at=now,
        )
        merged = _merge_model(base, filler, ["volume", "bid", "ask"])
        assert merged is base  # unchanged — base already complete

    def test_merge_does_not_overwrite_existing_value(self) -> None:
        now = datetime.now(UTC)
        base = Quote(
            symbol="AAPL", timestamp=now, price=150.0, volume=100, source="finnhub", fetched_at=now
        )
        filler = Quote(
            symbol="AAPL",
            timestamp=now,
            price=99.0,
            volume=9_999_999,
            source="yfinance",
            fetched_at=now,
        )
        merged = _merge_model(base, filler, ["volume"])
        # base.volume == 100 (non-zero) → should NOT be overwritten
        assert merged.volume == 100
        assert merged is base


# ---------------------------------------------------------------------------
# ProviderRouter — null-fill augment
# ---------------------------------------------------------------------------


def _make_config_with_augment(
    tiers: dict[str, list[str] | dict[str, list[str]]] | None = None,
    augment_fields: dict[str, list[str]] | None = None,
    augment_enabled: bool = True,
) -> OneFinanceConfig:
    return OneFinanceConfig(
        tiers=tiers
        or {
            "quote": ["prov_a", "prov_b"],
        },
        cooldown=CooldownConfig(default_initial_s=60.0, max_backoff_s=3600.0),
        augment=AugmentConfig(
            enabled=augment_enabled,
            fields=augment_fields or {"quote": ["volume", "bid", "ask"]},
        ),
    )


class MockQuoteProvider(BaseProvider):
    """Provider that returns a configurable Quote."""

    def __init__(self, name: str, *, volume: int, bid: float | None = None) -> None:
        self.name = name
        self._volume = volume
        self._bid = bid
        self.call_count = 0

    def get_quote(self, symbol: str) -> Quote:
        self.call_count += 1
        return Quote(
            symbol=symbol,
            timestamp=datetime.now(UTC),
            price=150.0,
            volume=self._volume,
            bid=self._bid,
            source=self.name,
            fetched_at=datetime.now(UTC),
        )

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 60.0


class TestRouterAugment:
    def test_augment_fills_volume_from_second_provider(self) -> None:
        prov_a = MockQuoteProvider("prov_a", volume=0)
        prov_b = MockQuoteProvider("prov_b", volume=5_000_000)
        config = _make_config_with_augment()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result: Quote = router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")

        assert result.volume == 5_000_000
        assert result.source == "prov_a+prov_b"
        assert prov_a.call_count == 1
        assert prov_b.call_count == 1  # called for augment

    def test_augment_not_triggered_when_primary_complete(self) -> None:
        # Only augment volume so we can test with a fully-populated volume field
        prov_a = MockQuoteProvider("prov_a", volume=1_000_000)
        prov_b = MockQuoteProvider("prov_b", volume=9_999_999)
        config = _make_config_with_augment(augment_fields={"quote": ["volume"]})
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result: Quote = router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")

        assert result.volume == 1_000_000
        assert result.source == "prov_a"
        assert prov_b.call_count == 0  # never called

    def test_augment_disabled_by_config(self) -> None:
        prov_a = MockQuoteProvider("prov_a", volume=0)
        prov_b = MockQuoteProvider("prov_b", volume=5_000_000)
        config = _make_config_with_augment(augment_enabled=False)
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result: Quote = router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")

        assert result.volume == 0  # not filled — augment disabled
        assert result.source == "prov_a"
        assert prov_b.call_count == 0

    def test_augment_skips_failing_filler_provider(self) -> None:
        """If the augment provider raises, the primary result is returned as-is."""
        prov_a = MockQuoteProvider("prov_a", volume=0)
        prov_b = FailingProvider("prov_b")
        config = _make_config_with_augment()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result: Quote = router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")

        assert result.volume == 0  # filler failed, primary result returned
        assert result.source == "prov_a"

    def test_augment_skips_endpoint_not_configured(self) -> None:
        """Augment only runs for endpoints listed in AugmentConfig.fields."""
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        prov_b = MockProvider("prov_b", supports_endpoints=["price_history"])
        # price_history is not in augment fields → no augment
        config = _make_config_with_augment(
            tiers={"price_history": ["prov_a", "prov_b"]},
            augment_fields={"quote": ["volume"]},  # only quote is augmented
        )
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_a"
        assert prov_b._call_count == 0  # not called for augment

    def test_augment_source_not_duplicated(self) -> None:
        """If filler provider name already appears in source, no duplicate."""
        prov_a = MockQuoteProvider("prov_a", volume=0)
        # Manually craft a provider that returns source already containing filler name
        config = _make_config_with_augment()
        prov_b = MockQuoteProvider("prov_b", volume=5_000_000)
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result: Quote = router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")

        # source should be "prov_a+prov_b", not "prov_a+prov_b+prov_b"
        assert result.source.count("prov_b") == 1


# ---------------------------------------------------------------------------
# ProviderRouter — fallback_order
# ---------------------------------------------------------------------------


class TestRouterFallbackOrder:
    """Tests for global fallback_order appended after the tier list."""

    def test_fallback_provider_tried_when_tier_fails(self) -> None:
        """Provider in fallback_order is tried after tier list exhausted."""
        prov_a = FailingProvider("prov_a")
        prov_fallback = MockProvider("prov_fallback", supports_endpoints=["price_history"])
        config = _make_config(
            tiers={"price_history": ["prov_a"]},
            fallback_order=["prov_fallback"],
        )
        router = ProviderRouter({"prov_a": prov_a, "prov_fallback": prov_fallback}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_fallback"

    def test_fallback_not_duplicated_when_already_in_tier(self) -> None:
        """If fallback provider is already in the tier list, it's not appended again."""
        prov_a = MockProvider("prov_a", supports_endpoints=["price_history"])
        prov_b = MockProvider("prov_b", supports_endpoints=["price_history"])
        config = _make_config(
            tiers={"price_history": ["prov_a", "prov_b"]},
            fallback_order=["prov_b"],  # prov_b already in tier
        )
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_a"
        assert prov_b._call_count == 0

    def test_fallback_not_registered_is_silently_skipped(self) -> None:
        """A fallback provider name not in the registry is ignored gracefully."""
        prov_a = FailingProvider("prov_a")
        prov_b = MockProvider("prov_b", supports_endpoints=["price_history"])
        config = _make_config(
            tiers={"price_history": ["prov_a"]},
            fallback_order=["ghost_provider", "prov_b"],
        )
        # ghost_provider not registered
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_b"

    def test_empty_fallback_order_no_extra_providers(self) -> None:
        """Empty fallback_order means no providers are appended beyond the tier list."""
        prov_a = FailingProvider("prov_a")
        prov_b = MockProvider("prov_b", supports_endpoints=["price_history"])
        config = _make_config(
            tiers={"price_history": ["prov_a"]},
            fallback_order=[],  # disabled
        )
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        with pytest.raises(AllProvidersFailedError):
            router.dispatch(
                "price_history",
                lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
            )

        assert prov_b._call_count == 0

    def test_multiple_fallbacks_tried_in_order(self) -> None:
        """Multiple fallback providers are tried in the given order."""
        prov_a = FailingProvider("prov_a")
        prov_fb1 = FailingProvider("prov_fb1")
        prov_fb2 = MockProvider("prov_fb2", supports_endpoints=["price_history"])
        config = _make_config(
            tiers={"price_history": ["prov_a"]},
            fallback_order=["prov_fb1", "prov_fb2"],
        )
        router = ProviderRouter(
            {"prov_a": prov_a, "prov_fb1": prov_fb1, "prov_fb2": prov_fb2}, config
        )

        result = router.dispatch(
            "price_history",
            lambda p: p.get_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31)),
        )

        assert result[0].source == "prov_fb2"


# ---------------------------------------------------------------------------
# Global-bench veto — a 402/403 on a symbol-gated plan must not bench the
# whole (provider, endpoint) when the endpoint recently succeeded (C1)
# ---------------------------------------------------------------------------


class RecordingNegativeCache:
    """Stateful RouterCache double that records negative/ok writes."""

    def __init__(self) -> None:
        self.global_neg: set[tuple[str, str]] = set()
        self.neg: set[tuple[str, str, str]] = set()
        self.ok: set[tuple[str, str]] = set()
        self.augments: dict[tuple[str, str], Any] = {}

    def get_negative_global(self, provider: str, endpoint: str) -> bool:
        return (provider, endpoint) in self.global_neg

    def get_negative(self, provider: str, endpoint: str, symbol: str | None) -> bool:
        return (provider, endpoint, (symbol or "").upper()) in self.neg

    def set_negative_global(self, provider: str, endpoint: str, ttl: int = 86400) -> None:
        self.global_neg.add((provider, endpoint))

    def set_negative(
        self, provider: str, endpoint: str, symbol: str | None, ttl: int = 86400
    ) -> None:
        self.neg.add((provider, endpoint, (symbol or "").upper()))

    def get_endpoint_ok(self, provider: str, endpoint: str) -> bool:
        return (provider, endpoint) in self.ok

    def mark_endpoint_ok(self, provider: str, endpoint: str, ttl: int = 86400) -> None:
        self.ok.add((provider, endpoint))
        self.global_neg.discard((provider, endpoint))

    def get_augment(self, endpoint: str, symbol: str) -> Any | None:
        return self.augments.get((endpoint, symbol.upper()))

    def set_augment(self, endpoint: str, symbol: str, value: Any, ttl: int = 300) -> None:
        self.augments[(endpoint, symbol.upper())] = value

    def get_router_state(self, provider: str) -> dict[str, object] | None:
        return None

    def set_router_state(
        self, provider: str, state: Mapping[str, object], ttl: int = 14400
    ) -> None:
        pass


class PlanGatedQuoteProvider(BaseProvider):
    """Quotes work except for plan-gated symbols, which 403 (NotSupportedError)."""

    def __init__(self, name: str, gated_symbols: set[str]) -> None:
        self.name = name
        self._gated = gated_symbols
        self.call_count = 0

    def get_quote(self, symbol: str) -> Quote:
        self.call_count += 1
        if symbol in self._gated:
            raise NotSupportedError(self.name, "quote", http_status=403)
        return _make_quote(symbol, source=self.name)

    def is_rate_limited(self, response: Any) -> bool:
        return False

    def cooldown_for(self, response: Any) -> float:
        return 60.0


class TestGlobalBenchVeto:
    def _router(
        self,
    ) -> tuple[ProviderRouter, PlanGatedQuoteProvider, MockProvider, RecordingNegativeCache]:
        gated = PlanGatedQuoteProvider("gated", gated_symbols={"000660.KS"})
        fallback = MockProvider("fallback", supports_endpoints=["quote"])
        config = _make_config(tiers={"quote": ["gated", "fallback"]})
        cache = RecordingNegativeCache()
        router = ProviderRouter({"gated": gated, "fallback": fallback}, config, cache=cache)
        return router, gated, fallback, cache

    def test_success_marks_endpoint_ok(self) -> None:
        router, _, _, cache = self._router()
        router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")
        assert ("gated", "quote") in cache.ok

    def test_403_after_success_stays_per_symbol(self) -> None:
        """A gated symbol's 403 must not bench the endpoint after a success."""
        router, gated, _, cache = self._router()
        router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")

        q = router.dispatch("quote", lambda p: p.get_quote("000660.KS"), symbol="000660.KS")
        assert q.source == "fallback"  # fell through to the next tier
        assert ("gated", "quote") not in cache.global_neg
        assert ("gated", "quote", "000660.KS") in cache.neg

        # Other symbols still reach the gated provider.
        q2 = router.dispatch("quote", lambda p: p.get_quote("NVDA"), symbol="NVDA")
        assert q2.source == "gated"

    def test_403_without_prior_success_benches_globally(self) -> None:
        """No success evidence → plan-gated endpoint benches globally (P4 behavior)."""
        router, gated, _, cache = self._router()
        q = router.dispatch("quote", lambda p: p.get_quote("000660.KS"), symbol="000660.KS")
        assert q.source == "fallback"
        assert ("gated", "quote") in cache.global_neg

        # Subsequent request for a different symbol skips the benched provider.
        calls_before = gated.call_count
        q2 = router.dispatch("quote", lambda p: p.get_quote("NVDA"), symbol="NVDA")
        assert q2.source == "fallback"
        assert gated.call_count == calls_before


# ---------------------------------------------------------------------------
# Augment budget (C2) — filler calls run under a total wall-clock budget
# ---------------------------------------------------------------------------


class SlowQuoteProvider(MockQuoteProvider):
    """Quote provider that sleeps before answering (simulates a slow filler)."""

    def __init__(self, name: str, *, volume: int, delay_s: float) -> None:
        super().__init__(name, volume=volume)
        self._delay_s = delay_s

    def get_quote(self, symbol: str) -> Quote:
        time.sleep(self._delay_s)
        return super().get_quote(symbol)


def _make_config_with_augment_timeout(timeout_s: float) -> OneFinanceConfig:
    return OneFinanceConfig(
        tiers={"quote": ["prov_a", "prov_b"]},
        cooldown=CooldownConfig(default_initial_s=60.0, max_backoff_s=3600.0),
        augment=AugmentConfig(
            enabled=True,
            timeout_s=timeout_s,
            fields={"quote": ["volume"]},
        ),
    )


class TestAugmentBudget:
    def test_slow_filler_times_out_and_primary_is_returned(self) -> None:
        prov_a = MockQuoteProvider("prov_a", volume=0)
        prov_b = SlowQuoteProvider("prov_b", volume=5_000_000, delay_s=0.5)
        config = _make_config_with_augment_timeout(0.05)
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        t0 = time.perf_counter()
        result: Quote = router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")
        elapsed = time.perf_counter() - t0

        assert result.volume == 0  # returned unaugmented
        assert result.source == "prov_a"
        assert elapsed < 0.4  # did not wait for the 0.5s filler

    def test_timed_out_filler_still_writes_augment_cache(self) -> None:
        prov_a = MockQuoteProvider("prov_a", volume=0)
        prov_b = SlowQuoteProvider("prov_b", volume=5_000_000, delay_s=0.15)
        config = _make_config_with_augment_timeout(0.02)
        cache = RecordingNegativeCache()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config, cache=cache)

        result: Quote = router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")
        assert result.volume == 0  # budget expired before the filler answered

        # The abandoned worker finishes in the background and caches its result.
        deadline = time.perf_counter() + 2.0
        while ("quote", "AAPL") not in cache.augments and time.perf_counter() < deadline:
            time.sleep(0.01)
        assert ("quote", "AAPL") in cache.augments

        # The next request merges from the augment cache without a new call.
        calls_before = prov_b.call_count
        result2: Quote = router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")
        assert result2.volume == 5_000_000
        assert prov_b.call_count == calls_before

    def test_fast_filler_within_budget_still_fills(self) -> None:
        prov_a = MockQuoteProvider("prov_a", volume=0)
        prov_b = MockQuoteProvider("prov_b", volume=5_000_000)
        config = _make_config_with_augment_timeout(2.0)
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config)

        result: Quote = router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")

        assert result.volume == 5_000_000
        assert result.source == "prov_a+prov_b"


# ---------------------------------------------------------------------------
# Request-level failure audit (C4) — all_failed terminal row
# ---------------------------------------------------------------------------


class RecordingAuditSink:
    """Audit sink double that captures entries in memory."""

    enabled = True

    def __init__(self) -> None:
        self.entries: list[Any] = []

    def record(self, entry: Any) -> None:
        self.entries.append(entry)


class TestAllFailedAudit:
    def test_exhausted_tier_records_all_failed_row(self) -> None:
        prov_a = FailingProvider("prov_a")
        prov_b = FailingProvider("prov_b")
        config = _make_config(tiers={"quote": ["prov_a", "prov_b"]})
        sink = RecordingAuditSink()
        router = ProviderRouter({"prov_a": prov_a, "prov_b": prov_b}, config, audit_log=sink)

        with pytest.raises(AllProvidersFailedError):
            router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")

        rows = [e for e in sink.entries if e.status == "all_failed"]
        assert len(rows) == 1
        row = rows[0]
        assert row.provider == "router"
        assert row.endpoint == "quote"
        assert row.symbol == "AAPL"
        assert row.error_code == "ALL_PROVIDERS_FAILED"
        # Terminal row shares the request_id with the per-provider attempts.
        assert row.request_id == sink.entries[0].request_id

    def test_successful_dispatch_records_no_all_failed_row(self) -> None:
        prov_a = MockProvider("prov_a", supports_endpoints=["quote"])
        config = _make_config(tiers={"quote": ["prov_a"]})
        sink = RecordingAuditSink()
        router = ProviderRouter({"prov_a": prov_a}, config, audit_log=sink)

        router.dispatch("quote", lambda p: p.get_quote("AAPL"), symbol="AAPL")

        assert not [e for e in sink.entries if e.status == "all_failed"]
