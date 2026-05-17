"""Provider config + liveness validation for ``ofclient providers check``.

The helper is pure-Python and offline by default. With ``ping=True`` it
calls each instantiated provider's lightest endpoint (``get_quote``).
"""

from __future__ import annotations

import os
import time
from typing import Any

from onefinance.core.config import OneFinanceConfig
from onefinance.core.errors import FinanceError
from onefinance.providers.base import BaseProvider


def _flatten_tier_refs(
    tiers: dict[str, list[str] | dict[str, list[str]]],
) -> dict[str, list[str]]:
    """Flatten tier entries into ``{endpoint: [provider, ...]}``.

    Tier values may be either ``list[str]`` (Type A/B) or
    ``{"default": [...], "fresh": [...]}`` (Type C). Both shapes
    collapse to a deduplicated, order-preserving provider list.
    """
    out: dict[str, list[str]] = {}
    for endpoint, entry in tiers.items():
        if isinstance(entry, dict):
            names: list[str] = []
            for sub in entry.values():
                if isinstance(sub, list):
                    names.extend(sub)
            out[endpoint] = list(dict.fromkeys(names))
        elif isinstance(entry, list):
            out[endpoint] = list(entry)
    return out


def check_providers_health(
    config: OneFinanceConfig,
    provider_map: dict[str, BaseProvider],
    *,
    ping: bool = False,
    ping_symbol: str = "AAPL",
    ping_timeout_s: float = 5.0,
    only: str | None = None,
) -> dict[str, Any]:
    """Return a structured health report for every configured provider.

    Parameters
    ----------
    config:
        Parsed :class:`OneFinanceConfig`.
    provider_map:
        Already-instantiated providers from
        :attr:`OneFinanceClient._provider_map`. Anything in
        ``config.providers`` but missing here is reported as
        ``not_instantiable`` (likely missing API key or import failure).
    ping:
        When ``True``, call ``get_quote(ping_symbol)`` on each
        instantiated provider and capture success/latency/error.
    ping_symbol:
        Symbol used for the ping. Defaults to ``"AAPL"``.
    ping_timeout_s:
        Informational — providers honour their own ``timeout_s`` from
        config; this value is recorded in the report for transparency.
    only:
        If set, restrict the report to a single provider name.
    """
    tier_refs = _flatten_tier_refs(config.tiers)
    known_names = set(config.providers.keys())

    tier_issues: list[dict[str, str]] = [
        {"endpoint": endpoint, "unknown_provider": ref}
        for endpoint, refs in tier_refs.items()
        for ref in refs
        if ref not in known_names
    ]

    providers: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "total": 0,
        "ok": 0,
        "missing_api_key": 0,
        "not_instantiable": 0,
        "unused": 0,
        "ping_failed": 0,
        "pings_succeeded": 0,
        "pings_failed": 0,
        "pings_attempted": ping,
        "ping_timeout_s": ping_timeout_s if ping else None,
    }

    for name, prov_cfg in config.providers.items():
        if only and name != only:
            continue
        summary["total"] += 1

        # ── Config checks ───────────────────────────────────────────
        api_key_env = prov_cfg.api_key_env
        api_key_present = True if api_key_env is None else bool(os.environ.get(api_key_env))
        instantiated = name in provider_map
        endpoints_using = [ep for ep, refs in tier_refs.items() if name in refs]
        in_use_in_tier = bool(endpoints_using)

        # ── Status classification ───────────────────────────────────
        if api_key_env and not api_key_present:
            status = "missing_api_key"
        elif not instantiated:
            status = "not_instantiable"
        elif not in_use_in_tier:
            status = "unused"
        else:
            status = "ok"

        # ── Optional ping ───────────────────────────────────────────
        ping_result: dict[str, Any] = {
            "attempted": False,
            "ok": None,
            "latency_ms": None,
            "endpoint": None,
            "symbol": None,
            "error": None,
        }
        if ping and instantiated:
            ping_result["attempted"] = True
            ping_result["endpoint"] = "quote"
            ping_result["symbol"] = ping_symbol
            start = time.perf_counter()
            try:
                provider_map[name].get_quote(ping_symbol)
                ping_result["ok"] = True
            except FinanceError as exc:
                ping_result["ok"] = False
                ping_result["error"] = {
                    "code": exc.code,
                    "message": exc.message,
                }
                if status == "ok":
                    status = "ping_failed"
            except Exception as exc:  # noqa: BLE001
                ping_result["ok"] = False
                ping_result["error"] = {
                    "code": "UNEXPECTED",
                    "message": f"{type(exc).__name__}: {exc}",
                }
                if status == "ok":
                    status = "ping_failed"
            ping_result["latency_ms"] = round(
                (time.perf_counter() - start) * 1000,
                2,
            )
            if ping_result["ok"]:
                summary["pings_succeeded"] += 1
            else:
                summary["pings_failed"] += 1

        summary[status] = summary.get(status, 0) + 1

        providers.append(
            {
                "name": name,
                "config": {
                    "api_key_env": api_key_env,
                    "api_key_present": api_key_present,
                    "instantiable": instantiated,
                    "in_use_in_tier": in_use_in_tier,
                    "tier_endpoints": endpoints_using,
                },
                "ping": ping_result,
                "status": status,
            }
        )

    return {
        "providers": providers,
        "tier_issues": tier_issues,
        "summary": summary,
    }
