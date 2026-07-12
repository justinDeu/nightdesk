"""Cost extraction + computation."""
import pytest

from nightdesk.domain.cost import (
    compute_cost,
    compute_cost_from_snapshot,
    compute_cost_with_softened_fallback,
    extract_usage,
)


def test_compute_cost_sonnet():
    # 1M input tokens at $3 = $3.00
    cost = compute_cost(
        model="claude-sonnet-4-5",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    assert cost == pytest.approx(3.0)


def test_compute_cost_opus_mixed():
    # 100k input + 50k output + 200k cache read on opus
    cost = compute_cost(
        model="claude-opus-4-1",
        input_tokens=100_000,
        output_tokens=50_000,
        cache_read_tokens=200_000,
        cache_write_tokens=0,
    )
    expected = (100_000 * 15.0 + 50_000 * 75.0 + 200_000 * 1.50) / 1_000_000.0
    assert cost == pytest.approx(expected)


def test_compute_cost_unknown_model_returns_none():
    # "glm-5.1" used to be unknown here (it was filtered out entirely in the
    # Anthropic-only era); now that the bundled table carries z.ai rows, it
    # genuinely resolves -- use a model with no bundled row at all instead.
    assert compute_cost(
        model="totally-unknown-model-xyz",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    ) is None


def test_compute_cost_glm_is_no_longer_discarded():
    # Regression guard: GLM rows used to be dropped by the old Anthropic-only
    # filter. They are now bundled directly in _PRICE_TABLE.
    cost = compute_cost(
        model="glm-5.2",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    assert cost == pytest.approx(1.40)


def test_compute_cost_glm_variant_suffix_is_normalized():
    # z.ai reports context-variant ids like "glm-5.2[1m]" for the long-context
    # tier. compute_cost must resolve these to the same bundled row as the
    # bare "glm-5.2" id (mirrors the normalization domain.pricing already
    # applies for the live/cached vendor-aware chain -- see
    # domain.pricing._model_candidates).
    cost = compute_cost(
        model="glm-5.2[1m]",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    assert cost == pytest.approx(1.40)


def test_compute_cost_vendor_prefixed_model_is_normalized():
    # A registry-style "vendor/model" id (e.g. surfaced by some compat
    # endpoints/routers) must still resolve against the bundled table.
    cost = compute_cost(
        model="zai/glm-5.2",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    assert cost == pytest.approx(1.40)


def test_compute_cost_longest_prefix_wins_regardless_of_table_order():
    # glm-5-code and glm-5 both prefix-match "glm-5-code-preview"; the
    # longer, more specific row must win even though the table lists
    # "glm-5-code" after "glm-5.2"/"glm-5.1" (see _prices_for's docstring).
    cost = compute_cost(
        model="glm-5-code-preview",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    assert cost == pytest.approx(1.20)  # glm-5-code's input rate, not glm-5's


def test_extract_usage_from_direct_usage_field():
    evt = {
        "type": "result",
        "model": "claude-sonnet-4-5",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 10,
        },
    }
    usage = extract_usage(evt)
    assert usage.model == "claude-sonnet-4-5"
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_read_tokens == 200
    assert usage.cache_write_tokens == 10
    assert usage.cost_usd is not None
    assert usage.cost_usd > 0


def test_extract_usage_with_no_usage_defaults_to_zero():
    evt = {"type": "result", "model": "claude-opus-4-1"}
    usage = extract_usage(evt)
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cost_usd == 0.0


def test_extract_usage_falls_back_to_model_hint():
    evt = {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}
    usage = extract_usage(evt, model_hint="claude-haiku-4-5")
    assert usage.model == "claude-haiku-4-5"
    assert usage.cost_usd is not None


# --- compute_cost_from_snapshot --------------------------------------------
def test_compute_cost_from_snapshot_single_model():
    snapshot = {"claude-sonnet-4-5": {
        "vendor": "anthropic", "input": 3.0, "output": 15.0,
        "cache_read": 0.3, "cache_write": 3.75,
        "source": "bundled", "as_of": "2026-07-05",
    }}
    usage = {"claude-sonnet-4-5": {"input_tokens": 1_000_000, "output_tokens": 0}}
    assert compute_cost_from_snapshot(usage, snapshot) == pytest.approx(3.0)


def test_compute_cost_from_snapshot_sums_across_vendors():
    snapshot = {
        "claude-sonnet-4-5": {
            "vendor": "anthropic", "input": 3.0, "output": 15.0,
            "cache_read": 0.3, "cache_write": 3.75,
            "source": "bundled", "as_of": "2026-07-05",
        },
        "glm-5.2": {
            "vendor": "zai", "input": 1.40, "output": 4.40,
            "cache_read": 0.26, "cache_write": 0.0,
            "source": "bundled", "as_of": "2026-07-05",
        },
    }
    usage = {
        "claude-sonnet-4-5": {"input_tokens": 1_000_000, "output_tokens": 0},
        "glm-5.2": {"input_tokens": 1_000_000, "output_tokens": 0},
    }
    assert compute_cost_from_snapshot(usage, snapshot) == pytest.approx(3.0 + 1.40)


def test_compute_cost_from_snapshot_missing_model_propagates_none():
    snapshot = {"claude-sonnet-4-5": {
        "vendor": "anthropic", "input": 3.0, "output": 15.0,
        "cache_read": 0.3, "cache_write": 3.75,
        "source": "bundled", "as_of": "2026-07-05",
    }}
    usage = {
        "claude-sonnet-4-5": {"input_tokens": 100, "output_tokens": 0},
        "unpriced-model": {"input_tokens": 100, "output_tokens": 0},
    }
    assert compute_cost_from_snapshot(usage, snapshot) is None


def test_compute_cost_from_snapshot_null_rate_entry_propagates_none():
    # An entry that was attempted but failed to resolve (source="none").
    snapshot = {"unpriced-model": {
        "vendor": "unknown", "input": None, "output": None,
        "cache_read": None, "cache_write": None,
        "source": "none", "as_of": "2026-07-05",
    }}
    usage = {"unpriced-model": {"input_tokens": 100, "output_tokens": 0}}
    assert compute_cost_from_snapshot(usage, snapshot) is None


def test_compute_cost_from_snapshot_empty_usage_is_zero():
    assert compute_cost_from_snapshot({}, {}) == 0.0


# --- compute_cost_with_softened_fallback ------------------------------------
_GPT_ENTRY = {
    "vendor": "openai", "input": 5.0, "output": 20.0,
    "cache_read": 0.5, "cache_write": 0.0,
    "source": "bundled", "as_of": "2026-07-05",
}
_GLM_ENTRY = {
    "vendor": "zai", "input": 1.40, "output": 4.40,
    "cache_read": 0.26, "cache_write": 0.0,
    "source": "bundled", "as_of": "2026-07-05",
}
_NULL_ENTRY = {
    "vendor": "unknown", "input": None, "output": None,
    "cache_read": None, "cache_write": None,
    "source": "none", "as_of": "2026-07-05",
}


def test_softened_fallback_prices_all_models_directly_when_all_have_rates():
    snapshot = {"gpt-5.4": _GPT_ENTRY, "glm-5.2": _GLM_ENTRY}
    usage = {
        "gpt-5.4": {"input_tokens": 1_000_000, "output_tokens": 0},
        "glm-5.2": {"input_tokens": 1_000_000, "output_tokens": 0},
    }
    cost, softened = compute_cost_with_softened_fallback(
        usage, snapshot, primary_model="gpt-5.4",
    )
    assert cost == pytest.approx(5.0 + 1.40)
    assert softened == []


def test_softened_fallback_prices_unrated_secondary_at_primary_rates():
    snapshot = {"gpt-5.4": _GPT_ENTRY, "unpriced-agent-model": _NULL_ENTRY}
    usage = {
        "gpt-5.4": {"input_tokens": 1_000_000, "output_tokens": 0},
        "unpriced-agent-model": {"input_tokens": 1_000_000, "output_tokens": 0},
    }
    cost, softened = compute_cost_with_softened_fallback(
        usage, snapshot, primary_model="gpt-5.4",
    )
    # Both priced at the primary's (gpt-5.4) rates: 5.0 per 1M input, twice.
    assert cost == pytest.approx(5.0 + 5.0)
    assert softened == ["unpriced-agent-model"]


def test_softened_fallback_returns_none_when_nothing_is_priceable():
    snapshot = {"gpt-5.4": _NULL_ENTRY, "unpriced-agent-model": _NULL_ENTRY}
    usage = {
        "gpt-5.4": {"input_tokens": 100, "output_tokens": 0},
        "unpriced-agent-model": {"input_tokens": 100, "output_tokens": 0},
    }
    cost, softened = compute_cost_with_softened_fallback(
        usage, snapshot, primary_model="gpt-5.4",
    )
    assert cost is None
    assert softened == []


def test_softened_fallback_skips_model_when_primary_also_unpriceable():
    """A secondary model with no rate and a primary that ALSO has no rate is
    excluded from the sum rather than crashing; other priceable models still
    count."""
    snapshot = {
        "gpt-5.4": _NULL_ENTRY,
        "glm-5.2": _GLM_ENTRY,
        "unpriced-agent-model": _NULL_ENTRY,
    }
    usage = {
        "gpt-5.4": {"input_tokens": 100, "output_tokens": 0},
        "glm-5.2": {"input_tokens": 1_000_000, "output_tokens": 0},
        "unpriced-agent-model": {"input_tokens": 100, "output_tokens": 0},
    }
    cost, softened = compute_cost_with_softened_fallback(
        usage, snapshot, primary_model="gpt-5.4",
    )
    assert cost == pytest.approx(1.40)  # only glm-5.2 was priceable
    assert softened == []


def test_softened_fallback_no_primary_model_set():
    """No primary model at all (e.g. legacy run) -- unrated models are
    simply excluded, never crash on a missing snapshot key."""
    snapshot = {"glm-5.2": _GLM_ENTRY, "unpriced-agent-model": _NULL_ENTRY}
    usage = {
        "glm-5.2": {"input_tokens": 1_000_000, "output_tokens": 0},
        "unpriced-agent-model": {"input_tokens": 100, "output_tokens": 0},
    }
    cost, softened = compute_cost_with_softened_fallback(
        usage, snapshot, primary_model=None,
    )
    assert cost == pytest.approx(1.40)
    assert softened == []
