"""Unit tests for TradierProvider with mocked httpx responses."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from onefinance.core.errors import ConfigError, ProviderError, RateLimitError
from onefinance.core.models import OptionChain
from onefinance.providers.tradier import _PROD_BASE, _SANDBOX_BASE, TradierProvider


@pytest.fixture
def provider() -> TradierProvider:
    return TradierProvider(api_key="test_token", timeout=5)


def _mock_response(data: Any, status_code: int = 200) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data) if not isinstance(data, str) else data
    resp.headers = {}
    return resp


_CHAIN_ROW_CALL = {
    "symbol": "SOXX260116C00200000",
    "underlying": "SOXX",
    "option_type": "call",
    "strike": 200.0,
    "bid": 5.0,
    "ask": 5.4,
    "last": 5.2,
    "volume": 10,
    "open_interest": 100,
    "greeks": {
        "delta": 0.55,
        "gamma": 0.02,
        "theta": -0.10,
        "vega": 0.30,
        "rho": 0.05,
        "mid_iv": 0.25,
        "smv_vol": 0.24,
    },
}
_CHAIN_ROW_PUT = {
    "symbol": "SOXX260116P00180000",
    "underlying": "SOXX",
    "option_type": "put",
    "strike": 180.0,
    "bid": 3.0,
    "ask": 3.2,
    "last": 3.1,
    "volume": 7,
    "open_interest": 50,
    "greeks": {"delta": -0.40, "gamma": 0.018, "mid_iv": 0.27, "smv_vol": 0.26},
}


# -----------------------------------------------------------------------
# Constructor
# -----------------------------------------------------------------------


class TestConstructor:
    def test_api_key_from_param(self) -> None:
        assert TradierProvider(api_key="my_token")._api_key == "my_token"

    def test_api_key_from_env(self) -> None:
        with patch.dict("os.environ", {"TRADIER_TOKEN": "env_token"}, clear=True):
            assert TradierProvider()._api_key == "env_token"

    def test_missing_token_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ConfigError):
                TradierProvider()

    def test_name(self) -> None:
        assert TradierProvider(api_key="k").name == "tradier"

    def test_defaults_to_sandbox(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert TradierProvider(api_key="k")._base_url == _SANDBOX_BASE

    def test_sandbox_env_falsy_uses_prod(self) -> None:
        with patch.dict("os.environ", {"TRADIER_SANDBOX": "0"}, clear=True):
            assert TradierProvider(api_key="k")._base_url == _PROD_BASE

    def test_explicit_base_url_overrides(self) -> None:
        p = TradierProvider(api_key="k", base_url="https://example.test/v1")
        assert p._base_url == "https://example.test/v1"


# -----------------------------------------------------------------------
# _get — HTTP error handling + rate limit
# -----------------------------------------------------------------------


class TestGetMethod:
    def test_429_raises_rate_limit(self, provider: TradierProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 429)):
            with pytest.raises(RateLimitError):
                provider._get("/markets/options/expirations", {"symbol": "SOXX"})

    def test_401_raises_auth_error(self, provider: TradierProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 401)):
            with pytest.raises(ProviderError) as exc_info:
                provider._get("/x", {})
        assert exc_info.value.code == "AUTH_ERROR"
        assert exc_info.value.retry_safe is False

    def test_500_raises_network_error(self, provider: TradierProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({}, 500)):
            with pytest.raises(ProviderError) as exc_info:
                provider._get("/x", {})
        assert exc_info.value.code == "NETWORK_ERROR"
        assert exc_info.value.retry_safe is True

    def test_sends_bearer_header(self, provider: TradierProvider) -> None:
        mock_get = MagicMock(return_value=_mock_response({"expirations": {"date": []}}))
        with patch.object(provider._client, "get", mock_get):
            provider._get("/markets/options/expirations", {"symbol": "SOXX"})
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer test_token"
        assert kwargs["headers"]["Accept"] == "application/json"


# -----------------------------------------------------------------------
# get_options_expirations
# -----------------------------------------------------------------------


class TestExpirations:
    def test_returns_sorted_dates(self, provider: TradierProvider) -> None:
        data = {"expirations": {"date": ["2026-02-20", "2026-01-16"]}}
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            out = provider.get_options_expirations("SOXX")
        assert out == [date(2026, 1, 16), date(2026, 2, 20)]

    def test_single_scalar_date(self, provider: TradierProvider) -> None:
        data = {"expirations": {"date": "2026-01-16"}}
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            out = provider.get_options_expirations("SOXX")
        assert out == [date(2026, 1, 16)]

    def test_null_expirations_returns_empty(self, provider: TradierProvider) -> None:
        resp = _mock_response({"expirations": None})
        with patch.object(provider._client, "get", return_value=resp):
            assert provider.get_options_expirations("SOXX") == []


# -----------------------------------------------------------------------
# get_option_chain
# -----------------------------------------------------------------------


class TestOptionChain:
    def test_returns_chain_splits_calls_puts(self, provider: TradierProvider) -> None:
        data = {"options": {"option": [_CHAIN_ROW_CALL, _CHAIN_ROW_PUT]}}
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            chain = provider.get_option_chain("SOXX", date(2026, 1, 16))
        assert isinstance(chain, OptionChain)
        assert chain.source == "tradier"
        assert len(chain.calls) == 1
        assert len(chain.puts) == 1
        assert chain.calls[0].contract_symbol == "SOXX260116C00200000"

    def test_greeks_populated(self, provider: TradierProvider) -> None:
        data = {"options": {"option": [_CHAIN_ROW_CALL]}}
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            chain = provider.get_option_chain("SOXX", date(2026, 1, 16))
        c = chain.calls[0]
        assert c.gamma == 0.02
        assert c.delta == 0.55
        assert c.smv_vol == 0.24
        assert c.implied_volatility == 0.25  # mapped from greeks.mid_iv

    def test_greeks_true_passed(self, provider: TradierProvider) -> None:
        mock_get = MagicMock(return_value=_mock_response({"options": None}))
        with patch.object(provider._client, "get", mock_get):
            provider.get_option_chain("SOXX", date(2026, 1, 16))
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["greeks"] == "true"

    def test_null_options_returns_empty_chain(self, provider: TradierProvider) -> None:
        with patch.object(provider._client, "get", return_value=_mock_response({"options": None})):
            chain = provider.get_option_chain("SOXX", date(2026, 1, 16))
        assert chain.calls == [] and chain.puts == []

    def test_single_contract_dict_not_list(self, provider: TradierProvider) -> None:
        # Tradier returns `option` as a bare dict when the chain has one contract.
        data = {"options": {"option": _CHAIN_ROW_CALL}}
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            chain = provider.get_option_chain("SOXX", date(2026, 1, 16))
        assert len(chain.calls) == 1

    def test_malformed_row_skipped(self, provider: TradierProvider) -> None:
        # first row lacks strike -> skipped
        data = {"options": {"option": [{"option_type": "call"}, _CHAIN_ROW_CALL]}}
        with patch.object(provider._client, "get", return_value=_mock_response(data)):
            chain = provider.get_option_chain("SOXX", date(2026, 1, 16))
        assert len(chain.calls) == 1
