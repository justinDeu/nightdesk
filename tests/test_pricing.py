"""Live model pricing: normalization, cache, and the live→cache→table chain."""
from datetime import datetime, timedelta, timezone

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
