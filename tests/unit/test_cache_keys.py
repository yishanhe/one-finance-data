"""Unit tests for cache key generation."""

from __future__ import annotations

from datetime import date, datetime, timezone

from onefinance.cache.keys import make_key


class TestMakeKey:
    def test_basic_format(self):
        key = make_key("price_history", symbol="AAPL", start="2024-01-01")
        assert key.startswith("price_history:")
        assert len(key.split(":")[1]) == 16  # 16-char hex hash

    def test_deterministic(self):
        """Same inputs always produce the same key."""
        k1 = make_key("quote", symbol="AAPL")
        k2 = make_key("quote", symbol="AAPL")
        assert k1 == k2

    def test_argument_order_irrelevant(self):
        """Params are sorted before hashing, so order doesn't matter."""
        k1 = make_key("price_history", symbol="AAPL", start="2024-01-01", end="2024-12-31")
        k2 = make_key("price_history", end="2024-12-31", symbol="AAPL", start="2024-01-01")
        assert k1 == k2

    def test_different_params_different_keys(self):
        k1 = make_key("price_history", symbol="AAPL", start="2024-01-01")
        k2 = make_key("price_history", symbol="MSFT", start="2024-01-01")
        assert k1 != k2

    def test_different_data_types_different_keys(self):
        k1 = make_key("price_history", symbol="AAPL")
        k2 = make_key("quote", symbol="AAPL")
        assert k1 != k2

    def test_date_and_string_produce_same_key(self):
        """date(2024,1,1) and '2024-01-01' should hash identically."""
        k1 = make_key("price_history", symbol="AAPL", start=date(2024, 1, 1))
        k2 = make_key("price_history", symbol="AAPL", start="2024-01-01")
        assert k1 == k2

    def test_none_params_excluded(self):
        """None-valued params are stripped before hashing."""
        k1 = make_key("insider_trades", symbol="AAPL")
        k2 = make_key("insider_trades", symbol="AAPL", since=None)
        assert k1 == k2

    def test_provider_not_in_key(self):
        """Key is provider-agnostic — no provider param in the key."""
        k1 = make_key("price_history", symbol="AAPL", start="2024-01-01")
        k2 = make_key("price_history", symbol="AAPL", start="2024-01-01")
        assert k1 == k2  # identical — provider is never a param

    def test_boolean_param(self):
        k1 = make_key("ratios", symbol="AAPL", period="annual", fresh=True)
        k2 = make_key("ratios", symbol="AAPL", period="annual", fresh=False)
        assert k1 != k2

    def test_numeric_param(self):
        k1 = make_key("price_history", symbol="AAPL", limit=100)
        k2 = make_key("price_history", symbol="AAPL", limit=200)
        assert k1 != k2
