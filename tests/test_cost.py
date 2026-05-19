"""Cost extraction + computation."""
import pytest

from nightdesk.domain.cost import compute_cost, extract_usage


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
    assert compute_cost(
        model="glm-5.1",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    ) is None


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
