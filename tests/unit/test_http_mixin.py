"""Tests for HttpProviderMixin rate-limit + transport behaviour."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from onefinance.core.errors import ProviderError, RateLimitError
from onefinance.providers._http import HttpProviderMixin, first_or_dict


def _mock_response(
    status_code: int = 200,
    data: Any = None,
    headers: dict[str, str] | None = None,
    text: str | None = None,
) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = data if data is not None else {}
    resp.text = text if text is not None else (json.dumps(data) if data is not None else "")
    return resp


class _MixinHost(HttpProviderMixin):
    """Minimal HttpProviderMixin host so the mixin can be exercised in isolation."""

    name = "_test"


class _RetryAfterHost(_MixinHost):
    _default_rate_limit_cooldown_s = 42.0


class _CustomSignalHost(HttpProviderMixin):
    """Demonstrates a provider-specific rate-limit signal override."""

    name = "_custom"

    def _rate_limit_signals(self, resp: httpx.Response) -> tuple[bool, int | None]:
        if resp.status_code == 429:
            return True, None
        text = getattr(resp, "text", "") or ""
        if "Limit Reach" in text:
            return True, None
        return False, None


def test_429_with_retry_after_uses_header() -> None:
    host = _RetryAfterHost()
    resp = _mock_response(status_code=429, headers={"Retry-After": "120"})
    host._client = MagicMock()
    host._client.get.return_value = resp

    with pytest.raises(RateLimitError) as excinfo:
        host._request("GET", "https://example.test/")
    assert excinfo.value.retry_after_seconds == 120


def test_429_without_retry_after_uses_default() -> None:
    host = _RetryAfterHost()
    resp = _mock_response(status_code=429, headers={})
    host._client = MagicMock()
    host._client.get.return_value = resp

    with pytest.raises(RateLimitError) as excinfo:
        host._request("GET", "https://example.test/")
    assert excinfo.value.retry_after_seconds == 42


def test_custom_rate_limit_signal_via_body_string() -> None:
    host = _CustomSignalHost()
    resp = _mock_response(status_code=200, text="Limit Reach for the day")
    host._client = MagicMock()
    host._client.get.return_value = resp

    with pytest.raises(RateLimitError):
        host._request("GET", "https://example.test/")


def test_transport_error_translated_to_provider_error() -> None:
    host = _MixinHost()
    host._client = MagicMock()
    host._client.get.side_effect = httpx.ConnectTimeout("boom")

    with pytest.raises(ProviderError) as excinfo:
        host._request("GET", "https://example.test/")
    assert excinfo.value.code == "NETWORK_ERROR"
    assert excinfo.value.retry_safe is True


def test_200_response_passes_through_untouched() -> None:
    host = _MixinHost()
    resp = _mock_response(status_code=200, data={"ok": True})
    host._client = MagicMock()
    host._client.get.return_value = resp

    result = host._request("GET", "https://example.test/")
    assert result is resp


def test_first_or_dict_handles_list_dict_and_other() -> None:
    assert first_or_dict([{"a": 1}, {"b": 2}]) == {"a": 1}
    assert first_or_dict({"a": 1}) == {"a": 1}
    assert first_or_dict([]) == {}
    assert first_or_dict(None) == {}
    assert first_or_dict("string") == {}
