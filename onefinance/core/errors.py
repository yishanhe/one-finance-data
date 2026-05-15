"""Structured error hierarchy for OneFinance.

Every exception carries a stable error code, a human-readable message,
and guidance on what to do next — designed for both Python callers and
CLI/agent consumers (see design doc §12).
"""

from __future__ import annotations


class FinanceError(Exception):
    """Base exception for all OneFinance errors.

    Attributes:
        code: Stable identifier an agent can branch on (e.g. ``"PROVIDER_QUOTA_EXHAUSTED"``).
        message: Human-readable explanation.
        suggested_action: What the caller should do next.
        retry_safe: Whether retrying the same call is worth it.
        retry_after_seconds: Cooldown hint, if known.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        suggested_action: str | None = None,
        retry_safe: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggested_action = suggested_action
        self.retry_safe = retry_safe
        self.retry_after_seconds = retry_after_seconds

    def to_dict(self) -> dict:
        """Serialisable form for CLI JSON output."""
        return {
            "code": self.code,
            "message": self.message,
            "suggested_action": self.suggested_action,
            "retry_safe": self.retry_safe,
            "retry_after_seconds": self.retry_after_seconds,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ProviderError(FinanceError):
    """An error originating from a specific data provider."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider: str,
        suggested_action: str | None = None,
        retry_safe: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(
            code,
            message,
            suggested_action=suggested_action,
            retry_safe=retry_safe,
            retry_after_seconds=retry_after_seconds,
        )
        self.provider = provider


class NotSupportedError(ProviderError):
    """The requested endpoint is not implemented by this provider."""

    def __init__(self, provider: str, endpoint: str) -> None:
        super().__init__(
            code="NOT_SUPPORTED",
            message=f"Provider '{provider}' does not support endpoint '{endpoint}'",
            provider=provider,
            suggested_action=f"Try a different provider that supports '{endpoint}'",
            retry_safe=False,
        )
        self.endpoint = endpoint


class RateLimitError(ProviderError):
    """Provider rate limit or quota exhausted."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(
            code="PROVIDER_QUOTA_EXHAUSTED",
            message=message,
            provider=provider,
            suggested_action=(
                f"Wait {retry_after_seconds}s and retry"
                if retry_after_seconds
                else "Wait and retry later"
            ),
            retry_safe=True,
            retry_after_seconds=retry_after_seconds,
        )


class AllProvidersFailedError(FinanceError):
    """All tiers exhausted for this endpoint."""

    def __init__(
        self,
        endpoint: str,
        failures: list[tuple[str, FinanceError]],
        *,
        fallback_providers_available: list[str] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.failures = failures
        self.fallback_providers_available = fallback_providers_available or []

        # Build a useful message from individual failures
        details = "; ".join(f"{name}: {err.message}" for name, err in failures)
        message = f"All providers failed for '{endpoint}': {details}"

        # Compute retry advice from the longest cooldown among failures
        max_retry = max(
            (e.retry_after_seconds for _, e in failures if e.retry_after_seconds),
            default=None,
        )

        if self.fallback_providers_available:
            suggestion = (
                f"Wait for cooldown to expire; "
                f"providers that may recover: {', '.join(self.fallback_providers_available)}"
            )
        else:
            suggestion = "All configured providers exhausted. Check API keys and quotas."

        super().__init__(
            code="ALL_PROVIDERS_FAILED",
            message=message,
            suggested_action=suggestion,
            retry_safe=max_retry is not None,
            retry_after_seconds=max_retry,
        )


class InvalidArgumentError(FinanceError):
    """Bad date range, unknown period, etc."""

    def __init__(self, message: str) -> None:
        super().__init__(
            code="INVALID_ARGUMENT",
            message=message,
            retry_safe=False,
        )


class ConfigError(FinanceError):
    """Required API key not set, config file missing, etc."""

    def __init__(self, message: str) -> None:
        super().__init__(
            code="CONFIG_MISSING_KEY",
            message=message,
            suggested_action="Check your config.yaml and environment variables",
            retry_safe=False,
        )
