---
name: add-data-provider
description: Add a new financial data provider (new API source like Alpha Vantage, Polygon, Tiingo, IEX) to onefinance. Use whenever the user asks to "add a provider", "integrate <some financial API>", "wire up a new data source", or wants onefinance to pull from an API not already in onefinance/providers/ (currently fmp, finnhub, twelve_data, yfinance). Also use when extending an *existing* provider with a new endpoint method, since the registration/test/doc touch points overlap.
---

# Add a data provider to onefinance

A provider is a `BaseProvider` subclass that adapts one external API to onefinance's
unified Pydantic models. Adding one touches ~8 files; miss one and the provider either
doesn't get picked up by the router, or the docs/tests drift from reality (this has
happened before — see Gotchas).

Read `onefinance/providers/twelve_data.py` first as the reference implementation —
it's the smallest complete provider (price_history + quote only) and shows every
pattern below in ~250 lines. For a full-coverage provider, `fmp.py` or `finnhub.py`
are better templates.

## Decide scope first

Ask: does this provider support every endpoint, or a subset? Providers are not
required to implement everything — `BaseProvider` stubs every endpoint to raise
`NotSupportedError`, and `supports(endpoint)` uses reflection to detect overrides.
Implement only what the API actually offers; never fake an endpoint with synthetic
data.

Check `onefinance/providers/base.py` `_ENDPOINT_METHODS` for the full list of
endpoint names → method names (price_history, quote, quotes, financials, info,
ratios, earnings, insider_trades, dcf, news, corporate_actions,
institutional_holders, analyst_data, options_expirations, option_chain,
forward_estimates, screen_stocks, sector_overview, earnings_calendar,
short_interest, market_sentiment).

## File-by-file checklist

### 1. `onefinance/providers/<name>.py` — the adapter

```python
class NewProvider(HttpProviderMixin, BaseProvider):
    name = "new_provider"  # lowercase, snake_case — this string is the canonical id used everywhere else

    def __init__(self, api_key=None, timeout=10, base_url=_BASE_URL, http_client=None):
        self._api_key = api_key or os.environ.get("NEW_PROVIDER_API_KEY")
        if not self._api_key:
            raise ConfigError("NEW_PROVIDER_API_KEY not set...")
        super().__init__(timeout=float(timeout), http_client=http_client)

    def _rate_limit_signals(self, resp: httpx.Response) -> tuple[bool, int | None]:
        # override only if the API has quirks beyond plain HTTP 429 + Retry-After
        ...

    def get_price_history(self, symbol, start, end, interval="1d") -> list[PriceBar]:
        ...  # only implement the endpoints this API actually supports
```

- Mix in `HttpProviderMixin` (`onefinance/providers/_http.py`) for httpx-based providers — gives
  `self._request()`, generic 429 detection, and the rate-limit-error-raising plumbing.
  yfinance doesn't use this mixin since it wraps the `yfinance` package, not raw HTTP.
- Use `_safe_float` / `_safe_int` / `normalize_symbol` / `parse_iso_date` /
  `parse_iso_datetime_utc` / `utc_now` from `onefinance/providers/_utils.py` rather than
  reinventing parsing.
- Every model instance needs `source=self.name` and `fetched_at=utc_now()`.
- Wrap schema-validation failures so they surface as `ProviderError`, not a raw
  `ValidationError` — the router treats that the same as any other provider failure
  and falls through to the next tier.
- If the provider has no API key (e.g. it's a free scraper like yfinance), skip the
  `ConfigError` constructor check — see `yfinance_provider.py`.

### 2. `onefinance/providers/__init__.py` — register the factory

```python
def _build_new_provider(cfg: ProviderConfig, http_client: httpx.Client | None) -> BaseProvider | None:
    if not cfg.api_key:
        return None
    from onefinance.providers.new_provider import NewProvider
    return NewProvider(api_key=cfg.api_key, timeout=cfg.timeout_s, http_client=http_client)

register(ProviderSpec("new_provider", _build_new_provider, requires_api_key=True))
```

Lazy-import the provider class inside the builder closure (not at module top) so a
missing optional dependency for one provider never breaks import of the whole package.
Returning `None` (vs raising) is the "no API key configured, skip silently" signal —
`build_all()` in `_factory.py` relies on this.

### 3. `onefinance/core/config.py` — wire into defaults

Two edits in `_default_config()` / `DEFAULT_TIERS`:

- Add a `ProviderConfig(name="new_provider", api_key_env="NEW_PROVIDER_API_KEY", timeout_s=10)`
  entry to the `providers` dict in `_default_config()`.
- Add `"new_provider"` to every tier list in `DEFAULT_TIERS` for each endpoint this
  provider supports. Tier order matters — see the comment block above `DEFAULT_TIERS`
  for the ranking criteria (data quality, recency, quota economics, reliability).
  Type C endpoints (`ratios`, `earnings`) need entries in both the `default` and
  `fresh` sub-lists.

### 4. `onefinance/cache/manager.py` — only if adding a *new endpoint type*

Not needed when the new provider implements existing endpoints. Only touch
`_DEFAULT_TTLS` if this work also introduces an endpoint that didn't exist before.

### 5. Tests — `tests/unit/test_<name>_provider.py`

Mirror `tests/unit/test_twelve_data_provider.py` (subset) or `test_fmp_provider.py`
(full coverage): mock `provider._client.get` via `unittest.mock.patch.object`, one
test class per endpoint, plus rate-limit detection tests. Also add to
`tests/unit/test_provider_factory.py`:

```python
def test_new_provider_with_key(self) -> None:
    cfg = ProviderConfig(name="new_provider", api_key_env="NEW_PROVIDER_API_KEY", timeout_s=10)
    with patch.dict("os.environ", {"NEW_PROVIDER_API_KEY": "test_key"}):
        p = build("new_provider", cfg)
    assert p is not None and p.name == "new_provider"

def test_new_provider_without_key_returns_none(self) -> None:
    cfg = ProviderConfig(name="new_provider", api_key_env="NEW_PROVIDER_API_KEY", timeout_s=10)
    with patch.dict("os.environ", {}, clear=True):
        assert build("new_provider", cfg) is None
```

If live-network testing is warranted, add an `@pytest.mark.integration` test under
`tests/integration/` — these are excluded from normal CI runs (`-m "not integration"`).

### 6. `onefinance/cli/app.py` — `config_init` template

`ofclient config init` emits a hardcoded YAML string (search for `fmp:` / `finnhub:`
around line ~1777). Add the new provider's `api_key_env` block and append it to the
relevant tier lists in that same template string — this is separate from
`config.py`'s `DEFAULT_TIERS` and easy to forget since nothing fails loudly if it's
stale, the generated config is just incomplete.

### 7. Docs — README.md, CLAUDE.md

Both have a "Provider coverage" / "Provider capability matrix" table
(`| Endpoint | FMP | Finnhub | Twelve Data | YFinance |`). Add a column for the new
provider and a `✓`/`—` row entry per endpoint it supports. CLAUDE.md also has an
"Environment Variables" table — add the new `*_API_KEY` row, and a "Provider API
References" section — add a link to the new provider's API docs.

### 8. `pyproject.toml` — only if a new SDK dependency is needed

If the provider needs a client library (like `yfinance` does), add it via
`uv add <package>`. Pure-httpx providers (fmp, finnhub, twelve_data) need nothing new.

## Gotchas

- **Hardcoded endpoint counts in tests.** `tests/unit/test_fmp_provider.py` has
  `assert len(endpoints) == 17` against `FMPProvider().supported_endpoints`. This kind
  of count assertion exists per-provider for whichever provider happens to support the
  most endpoints — it silently goes stale and was the subject of a real fix
  (`bf9aeea fix: update FMP supported endpoint count to 17`). Grep for
  `len(endpoints)` or `supported_endpoints` in tests after adding endpoint coverage to
  any existing provider.
- **`name` is load-bearing.** The string in `ProviderSpec(name, ...)`, the class's
  `name` attribute, the key in `config.providers`, and the strings inside
  `DEFAULT_TIERS` must all match exactly — there's no validation step that catches a
  typo here at runtime, the provider will just silently never be selected by the
  router (`providers_check` CLI command will show it as `unused` if it's missing from
  tier tables, which is the fastest way to verify wiring).
- **Don't synthesize unsupported data.** If the API doesn't have a true equivalent for
  an endpoint, leave the `BaseProvider` stub (`NotSupportedError`) — don't approximate.
  The router and `supports()` reflection depend on accurate capability signaling.

## Verify

```bash
uv run pytest tests/unit/test_<name>_provider.py tests/unit/test_provider_factory.py -v
uv run ofclient providers check --provider <name>      # confirms api_key_present, instantiable, in_use_in_tier
uv run ruff check onefinance tests && uv run mypy onefinance tests
uv run pytest tests/ -m "not integration"               # full regression
```
