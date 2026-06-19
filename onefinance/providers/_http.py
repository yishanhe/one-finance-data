"""Shared HTTP client + rate-limit detection for httpx-based providers.

Providers that talk to a REST API mix in :class:`HttpProviderMixin` to get:

* a single ``_request`` entry point that wraps ``httpx.Client.request`` and
  translates transport errors into :class:`ProviderError`,
* a uniform ``_check_rate_limit`` hook called on every response, with a
  provider-specific override seam at :meth:`_rate_limit_signals`,
* the ability to swap in a shared/test ``httpx.Client`` via the
  ``http_client`` constructor argument.

Per-provider quirks (FMP's "Limit Reach" body string, TwelveData's JSON
envelope with ``code: 429``) live in the subclass ``_rate_limit_signals``
override rather than scattered inline checks.
"""

from __future__ import annotations

from typing import Any

import httpx

from onefinance.core.errors import ProviderError, RateLimitError


class HttpProviderMixin:
    """HTTP session + rate-limit detection for providers using ``httpx``.

    Subclasses are expected to set ``self.name`` (already required by
    :class:`BaseProvider`) and pick a sensible default rate-limit cooldown via
    :attr:`_default_rate_limit_cooldown_s`.
    """

    name: str
    _default_rate_limit_cooldown_s: float = 60.0

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._timeout = timeout
        self._client: httpx.Client = http_client or httpx.Client(
            timeout=timeout, follow_redirects=True
        )

    # ------------------------------------------------------------------
    # Request entry point
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue *method* to *url*; raise on transport + rate-limit failure."""
        try:
            method_lower = method.lower()
            if method_lower == "get":
                resp = self._client.get(url, params=params, headers=headers)
            else:
                resp = self._client.request(method, url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(
                code="NETWORK_ERROR",
                message=f"{self.name} request failed: {exc}",
                provider=self.name,
                retry_safe=True,
            ) from exc

        self._check_rate_limit(resp)
        return resp

    # ------------------------------------------------------------------
    # Rate-limit detection
    # ------------------------------------------------------------------

    def _check_rate_limit(self, resp: httpx.Response) -> None:
        """Raise :class:`RateLimitError` when the response signals throttling."""
        hit, retry_after = self._rate_limit_signals(resp)
        if not hit:
            return
        cooldown = retry_after if retry_after is not None else self._default_rate_limit_cooldown_s
        raise RateLimitError(
            provider=self.name,
            message=f"{self.name} rate limit hit (HTTP {resp.status_code})",
            retry_after_seconds=int(cooldown),
            http_status=resp.status_code,
        )

    def _rate_limit_signals(self, resp: httpx.Response) -> tuple[bool, int | None]:
        """Return ``(hit, retry_after_seconds)`` for *resp*.

        Default: HTTP 429 plus optional ``Retry-After`` header. Override in
        subclasses to add provider-specific signals.
        """
        if resp.status_code == 429:
            return True, _parse_retry_after(resp.headers.get("Retry-After"))
        return False, None


def _parse_retry_after(value: str | None) -> int | None:
    """Best-effort numeric parse of the ``Retry-After`` header."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def first_or_dict(data: Any) -> dict[str, Any]:
    """Return the first list element or *data* if already a dict; empty otherwise."""
    if isinstance(data, list):
        return data[0] if data else {}
    if isinstance(data, dict):
        return data
    return {}
