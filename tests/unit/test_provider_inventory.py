"""Tests for the shared provider diagnostic inventory."""

from __future__ import annotations

from pytest import MonkeyPatch

from onefinance.core._provider_inventory import ProviderInventory
from onefinance.core.config import OneFinanceConfig, ProviderConfig


def _config() -> OneFinanceConfig:
    return OneFinanceConfig(
        providers={
            "keyed": ProviderConfig(name="keyed", api_key_env="KEYED_API_KEY"),
            "keyless": ProviderConfig(name="keyless"),
            "unused": ProviderConfig(name="unused"),
        },
        tiers={
            "quote": ["keyed", "keyless"],
            "price_history": ["keyed", "missing"],
        },
    )


def test_registration_combines_config_runtime_and_tiers(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("KEYED_API_KEY", "secret")
    inventory = ProviderInventory(_config(), {"keyed": object(), "keyless": object()})

    registrations = {item.name: item for item in inventory.registrations()}

    assert registrations["keyed"].status == "ok"
    assert registrations["keyed"].tier_endpoints == ("quote", "price_history")
    assert registrations["keyless"].status == "ok"
    assert registrations["unused"].status == "not_instantiable"


def test_registration_status_prioritizes_missing_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("KEYED_API_KEY", raising=False)
    inventory = ProviderInventory(_config(), {})

    keyed = next(inventory.registrations(only="keyed"))

    assert keyed.status == "missing_api_key"


def test_unknown_tier_references_are_reported() -> None:
    inventory = ProviderInventory(_config(), {})

    assert list(inventory.unknown_tier_references()) == [
        ("price_history", "missing"),
    ]
