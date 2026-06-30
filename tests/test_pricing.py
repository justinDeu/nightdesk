"""Live model pricing: normalization, cache, and the live→cache→table chain."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nightdesk.domain import pricing
from nightdesk.domain.pricing import PriceInfo


NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


# --- normalization --------------------------------------------------------
def test_normalize_canonical_shape():
    data = {"prices": {"claude-opus-4": {
        "input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}}}
    out = pricing.normalize_endpoint_json(data)
    assert out == {"claude-opus-4": {
        "input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}}


def test_normalize_registry_aggregate_strips_provider_prefix():
    # models.dev-style: keyed by "provider/model" with a "cost" block.
    data = {
        "anthropic/claude-opus-4-7": {"cost": {
            "input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
            "name": "Claude Opus 4.7"},
        "anthropic/claude-sonnet-4-6": {"pricing": {
            "input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}},
    }
    out = pricing.normalize_endpoint_json(data)
    assert set(out) == {"claude-opus-4-7", "claude-sonnet-4-6"}
    assert out["claude-opus-4-7"]["input"] == 5.0


def test_normalize_flat_shape():
    data = {"claude-haiku-4": {
        "input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25}}
    out = pricing.normalize_endpoint_json(data)
    assert out["claude-haiku-4"]["output"] == 5.0


def test_normalize_skips_entries_missing_a_component():
    data = {"claude-opus-4": {"input": 5.0, "output": 25.0}}  # no cache fields
    assert pricing.normalize_endpoint_json(data) == {}


def test_normalize_accepts_aliases():
    data = {"claude-opus-4": {
        "input": 5.0, "output": 25.0,
        "cache_creation": 6.25, "cache_hit": 0.5}}
    out = pricing.normalize_endpoint_json(data)
    assert out["claude-opus-4"]["cache_write"] == 6.25
    assert out["claude-opus-4"]["cache_read"] == 0.5


def test_normalize_garbage_is_empty():
    assert pricing.normalize_endpoint_json(None) == {}
    assert pricing.normalize_endpoint_json("not json") == {}
    assert pricing.normalize_endpoint_json({}) == {}
    assert pricing.normalize_endpoint_json({"prices": "oops"}) == {}


# --- PriceInfo ------------------------------------------------------------
def test_priceinfo_longest_prefix_match():
    info = PriceInfo("live", "2026-06-30", {
        "claude-opus-4": {"input": 5.0, "output": 25.0,
                          "cache_read": 0.5, "cache_write": 6.25},
        "claude-opus-4-7": {"input": 9.0, "output": 45.0,
                            "cache_read": 0.9, "cache_write": 11.25},
    })
    # The more specific "claude-opus-4-7" must win over "claude-opus-4".
    assert info.prices_for("claude-opus-4-7-20260416")["input"] == 9.0
    # Falls back to the shorter prefix for a non-4.7 opus.
    assert info.prices_for("claude-opus-4-5")["input"] == 5.0


def test_priceinfo_unknown_model_costs_zero():
    info = pricing.table_price_info()
    assert info.prices_for("gpt-5") is None
    assert info.cost("gpt-5", 1_000_000, 0, 0, 0) == 0.0
    assert info.cost(None, 100, 100, 100, 100) == 0.0


def test_priceinfo_cost_matches_compute_cost_formula():
    info = pricing.table_price_info()
    # 1M input on sonnet at bundled $3 -> $3.00.
    assert info.cost("claude-sonnet-4-5", 1_000_000, 0, 0, 0) == pytest.approx(3.0)


def test_priceinfo_labels():
    assert "live prices" in PriceInfo("live", "2026-06-30", {}).label
    assert "cached prices" in PriceInfo("cache", "2026-06-30", {}).label
    assert "bundled prices" in PriceInfo("table", "2026-05-18", {}).label


# --- cache ----------------------------------------------------------------
def test_cache_roundtrip(tmp_path):
    path = pricing.cache_path(tmp_path)
    assert path is not None
    prices = {"claude-opus-4": {
        "input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}}
    pricing.write_cache(path, prices, source="http://x", fetched_at=NOW.isoformat())
    rec = pricing.read_cache(path)
    assert rec is not None
    got, fetched_at = rec
    assert got == prices
    assert fetched_at == NOW.isoformat()


def test_cache_missing_returns_none(tmp_path):
    assert pricing.read_cache(pricing.cache_path(tmp_path)) is None


def test_cache_corrupt_returns_none(tmp_path):
    path = pricing.cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert pricing.read_cache(path) is None


def test_cache_fresh_within_and_beyond_ttl(tmp_path):
    path = pricing.cache_path(tmp_path)
    pricing.write_cache(path, {"claude-opus-4": {
        "input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}},
        source="x", fetched_at=NOW.isoformat())
    assert pricing.cache_fresh(path, now=NOW) is True
    # 25h later -> stale.
    assert pricing.cache_fresh(path, now=NOW + timedelta(hours=25)) is False


# --- resolve_prices: the fallback chain -----------------------------------
LIVE_PRICES = {"claude-opus-4": {
    "input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}}


def _fetcher_returning(prices):
    calls = []
    def _fetch(url):
        calls.append(url)
        return prices
    return _fetch, calls


def _fetcher_failing():
    calls = []
    def _fetch(url):
        calls.append(url)
        return None
    return _fetch, calls


def test_resolve_live_wins_and_persists_cache(tmp_path):
    fetch, calls = _fetcher_returning(LIVE_PRICES)
    info = pricing.resolve_prices(tmp_path, url="http://x", now=NOW, fetcher=fetch)
    assert info.source == "live"
    assert info.prices == LIVE_PRICES
    assert calls == ["http://x"]
    # Live success writes the cache.
    rec = pricing.read_cache(pricing.cache_path(tmp_path))
    assert rec is not None and rec[0] == LIVE_PRICES


def test_resolve_fresh_cache_skips_network(tmp_path):
    # Seed a fresh cache.
    pricing.write_cache(pricing.cache_path(tmp_path), LIVE_PRICES,
                        source="http://x", fetched_at=NOW.isoformat())
    fetch, calls = _fetcher_returning({"claude-opus-4": {
        "input": 999.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0}})
    info = pricing.resolve_prices(tmp_path, url="http://x", now=NOW, fetcher=fetch)
    assert info.source == "cache"
    assert calls == []  # no fetch within TTL
    assert info.prices == LIVE_PRICES


def test_resolve_expired_cache_live_fails_falls_back_to_stale_cache(tmp_path):
    # Stale cache (2 days old).
    pricing.write_cache(pricing.cache_path(tmp_path), LIVE_PRICES,
                        source="http://x",
                        fetched_at=(NOW - timedelta(days=2)).isoformat())
    fetch, calls = _fetcher_failing()
    info = pricing.resolve_prices(tmp_path, url="http://x", now=NOW, fetcher=fetch)
    assert info.source == "cache"
    assert info.prices == LIVE_PRICES  # stale cache still beats the table
    assert calls == ["http://x"]


def test_resolve_no_cache_live_fails_falls_back_to_table(tmp_path):
    fetch, calls = _fetcher_failing()
    info = pricing.resolve_prices(tmp_path, url="http://x", now=NOW, fetcher=fetch)
    assert info.source == "table"
    assert info.as_of == "2026-05-18"  # PRICES_AS_OF


def test_resolve_url_none_never_fetches_uses_table(tmp_path):
    fetch, calls = _fetcher_returning(LIVE_PRICES)
    info = pricing.resolve_prices(tmp_path, url=None, now=NOW, fetcher=fetch)
    assert info.source == "table"
    assert calls == []  # no live attempt without a URL


def test_resolve_no_data_dir_no_url_uses_table():
    info = pricing.resolve_prices(None, url=None, now=NOW)
    assert info.source == "table"


def test_resolve_ttl_expiry_triggers_live_refetch(tmp_path):
    # Fresh cache.
    pricing.write_cache(pricing.cache_path(tmp_path), LIVE_PRICES,
                        source="http://x", fetched_at=NOW.isoformat())
    new_prices = {"claude-opus-4": {
        "input": 7.0, "output": 35.0, "cache_read": 0.7, "cache_write": 8.75}}
    fetch, _ = _fetcher_returning(new_prices)
    # Past TTL -> live is consulted again and overwrites the cache.
    info = pricing.resolve_prices(
        tmp_path, url="http://x", now=NOW + timedelta(hours=25), fetcher=fetch)
    assert info.source == "live"
    assert info.prices == new_prices
    rec = pricing.read_cache(pricing.cache_path(tmp_path))
    assert rec is not None and rec[0] == new_prices


# --- live fetch fail-soft -------------------------------------------------
def test_fetch_live_prices_fails_soft_on_unreachable():
    # Connection refused / unreachable -> None, never raises.
    out = pricing.fetch_live_prices("http://127.0.0.1:1/nope", timeout_seconds=0.5)
    assert out is None


def test_fetch_live_prices_empty_url_returns_none():
    assert pricing.fetch_live_prices("") is None


# --- default source: must carry real Anthropic pricing -------------------
def test_default_pricing_url_is_not_the_broken_models_dev_endpoint():
    # Regression guard for the original bug: models.dev/models.json carries no
    # pricing, so fetch_live_prices silently returned an empty map and the UI
    # fell through to the bundled table. The default must point at a source
    # that actually publishes per-token USD pricing for Claude models.
    assert "models.dev" not in pricing.DEFAULT_PRICING_URL
    assert pricing.DEFAULT_PRICING_URL.startswith(("http://", "https://"))


def test_default_url_live_fetch_returns_real_anthropic_prices():
    # Hits the REAL default endpoint. This is the test the original bug
    # lacked: it would have failed when models.dev yielded zero price entries.
    out = pricing.fetch_live_prices(pricing.DEFAULT_PRICING_URL, timeout_seconds=8.0)
    if out is None:
        pytest.skip("network unavailable — cannot verify the live default source")
    assert out, "default endpoint returned no prices (the original regression)"
    # Must contain Claude models.
    claude_keys = [k for k in out if "claude" in k.lower()]
    assert claude_keys, "no Claude models in default endpoint output"
    # Every price finite and in a sane per-1M range.
    for prefix, p in out.items():
        assert all(isinstance(v, (int, float)) for v in p.values()), prefix
        assert 0.1 <= p["input"] <= 200.0, (prefix, p)
        assert 0.5 <= p["output"] <= 200.0, (prefix, p)
        assert 0.0 < p["cache_read"] < p["input"], (prefix, p)
        assert 0.0 < p["cache_write"] <= p["input"] * 2.0, (prefix, p)


# --- parser: dict-keyed (LiteLLM) + list-under-data (OpenRouter) ---------
DATA_DIR = Path(__file__).parent / "data"


def _load_fixture(name: str) -> object:
    return json.loads((DATA_DIR / name).read_text())


def test_normalize_litellm_dict_keyed_scales_per_token_to_per_1m():
    data = _load_fixture("litellm_prices_sample.json")
    out = pricing.normalize_endpoint_json(data)
    # First-party anthropic entries survive; bedrock/openrouter resellers dropped.
    assert "claude-opus-4-5" in out
    assert "claude-opus-4-7" in out
    assert "claude-sonnet-4-5" in out
    # Reseller copies filtered out by litellm_provider.
    bedrock_key = "claude-opus-4-5-20251101-v1:0"
    openrouter_key = "claude-opus-4.5"
    assert bedrock_key not in out
    assert openrouter_key not in out
    # Per-token (5e-06) scaled to per-1M (5.0).
    p = out["claude-opus-4-5"]
    assert p == {
        "input": 5.0, "output": 25.0,
        "cache_read": 0.5, "cache_write": 6.25,
    }


def test_normalize_openrouter_list_under_data_scales_per_token_to_per_1m():
    data = _load_fixture("openrouter_models_sample.json")
    out = pricing.normalize_endpoint_json(data)
    # anthropic/ entries kept (provider/ prefix stripped); google/ dropped.
    assert "claude-opus-4.5" in out
    assert "claude-sonnet-4.5" in out
    assert not any(k.startswith("gemini") for k in out), "non-Anthropic leaked through"
    # OpenRouter publishes string per-token values; coerced + scaled to per-1M.
    p = out["claude-opus-4.5"]
    assert p["input"] == pytest.approx(5.0)
    assert p["output"] == pytest.approx(25.0)
    assert p["cache_read"] == pytest.approx(0.5)
    assert p["cache_write"] == pytest.approx(6.25)


def test_normalize_per_token_alias_mapping_is_complete():
    # Both alias families must round-trip every component when only the
    # per-token fields are present (no per-1M fallback).
    litellm = {"claude-opus-4-7": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 25e-06,
        "cache_read_input_token_cost": 5e-07,
        "cache_creation_input_token_cost": 6.25e-06,
        "litellm_provider": "anthropic",
    }}
    out = pricing.normalize_endpoint_json(litellm)
    assert out["claude-opus-4-7"] == {
        "input": 5.0, "output": 25.0,
        "cache_read": 0.5, "cache_write": 6.25,
    }

    openrouter = {"data": [{"id": "anthropic/claude-opus-4.7", "pricing": {
        "prompt": "0.000005", "completion": "0.000025",
        "input_cache_read": "0.0000005", "input_cache_write": "0.00000625",
    }}]}
    out = pricing.normalize_endpoint_json(openrouter)
    assert out["claude-opus-4.7"]["input"] == pytest.approx(5.0)
    assert out["claude-opus-4.7"]["cache_write"] == pytest.approx(6.25)
