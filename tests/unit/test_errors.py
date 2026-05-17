"""Unit tests for the errors module."""

from __future__ import annotations

from onefinance.core.errors import (
    AllProvidersFailedError,
    ConfigError,
    FinanceError,
    InvalidArgumentError,
    NotSupportedError,
    ProviderError,
    RateLimitError,
)


class TestFinanceError:
    def test_basic(self) -> None:
        err = FinanceError("TEST_CODE", "something broke")
        assert err.code == "TEST_CODE"
        assert err.message == "something broke"
        assert str(err) == "something broke"
        assert err.retry_safe is False
        assert err.retry_after_seconds is None

    def test_to_dict(self) -> None:
        err = FinanceError(
            "X",
            "msg",
            suggested_action="do Y",
            retry_safe=True,
            retry_after_seconds=60,
        )
        d = err.to_dict()
        assert d["code"] == "X"
        assert d["retry_safe"] is True
        assert d["retry_after_seconds"] == 60
        assert d["suggested_action"] == "do Y"


class TestProviderError:
    def test_provider_field(self) -> None:
        err = ProviderError("X", "msg", provider="fmp")
        assert err.provider == "fmp"


class TestNotSupportedError:
    def test_auto_fields(self) -> None:
        err = NotSupportedError("yfinance", "ratios")
        assert err.code == "NOT_SUPPORTED"
        assert "yfinance" in err.message
        assert "ratios" in err.message
        assert err.retry_safe is False
        assert err.endpoint == "ratios"


class TestRateLimitError:
    def test_with_cooldown(self) -> None:
        err = RateLimitError("fmp", "daily limit hit", retry_after_seconds=3600)
        assert err.retry_safe is True
        assert err.retry_after_seconds == 3600
        assert "3600" in (err.suggested_action or "")


class TestAllProvidersFailedError:
    def test_aggregates_failures(self) -> None:
        f1 = RateLimitError("fmp", "quota", retry_after_seconds=3600)
        f2 = NotSupportedError("yfinance", "ratios")
        err = AllProvidersFailedError(
            endpoint="ratios",
            failures=[("fmp", f1), ("yfinance", f2)],
            fallback_providers_available=["finnhub"],
        )
        assert err.code == "ALL_PROVIDERS_FAILED"
        assert len(err.failures) == 2
        assert err.fallback_providers_available == ["finnhub"]
        assert err.retry_safe is True
        assert err.retry_after_seconds == 3600


class TestInvalidArgumentError:
    def test_basic(self) -> None:
        err = InvalidArgumentError("start date after end date")
        assert err.code == "INVALID_ARGUMENT"
        assert err.retry_safe is False


class TestConfigError:
    def test_basic(self) -> None:
        err = ConfigError("FMP_API_KEY not set")
        assert err.code == "CONFIG_MISSING_KEY"
        assert err.retry_safe is False
