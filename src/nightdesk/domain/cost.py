"""Per-run cost computation for CC runs.

The CC SDK emits a final ``result`` event with a ``usage`` block:
``{"input_tokens", "output_tokens", "cache_creation_input_tokens",
"cache_read_input_tokens"}``. We persist those counts onto the Run row
and compute a USD estimate using a small in-repo price table.

Prices change. The table here is correct as of 2026-05-18; update it
when the published prices move. The UI surfaces "as of YYYY-MM-DD"
next to the cost so the user knows it's an estimate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


PRICES_AS_OF = "2026-05-18"


# USD per 1M tokens. Sourced from the Anthropic pricing page.
# Model-id matching is prefix-based ("claude-opus-4" matches all opus-4 variants).
_PRICE_TABLE: list[tuple[str, dict[str, float]]] = [
    ("claude-opus-4", {
        "input": 15.0, "output": 75.0,
        "cache_write": 18.75, "cache_read": 1.50,
    }),
    ("claude-sonnet-4", {
        "input": 3.0, "output": 15.0,
        "cache_write": 3.75, "cache_read": 0.30,
    }),
    ("claude-haiku-4", {
        "input": 1.0, "output": 5.0,
        "cache_write": 1.25, "cache_read": 0.10,
    }),
    # Older 3.x lines kept for back-compat with stale profiles.
    ("claude-3-opus", {
        "input": 15.0, "output": 75.0,
        "cache_write": 18.75, "cache_read": 1.50,
    }),
    ("claude-3-5-sonnet", {
        "input": 3.0, "output": 15.0,
        "cache_write": 3.75, "cache_read": 0.30,
    }),
    ("claude-3-5-haiku", {
        "input": 1.0, "output": 5.0,
        "cache_write": 1.25, "cache_read": 0.10,
    }),
]


@dataclass
class RunUsage:
    model: Optional[str]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Optional[float]


def _prices_for(model: Optional[str]) -> Optional[dict[str, float]]:
    if not model:
        return None
    for prefix, prices in _PRICE_TABLE:
        if model.startswith(prefix):
            return prices
    return None


def compute_cost(
    *,
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> Optional[float]:
    """Return USD cost or None if the model is unknown."""
    prices = _prices_for(model)
    if prices is None:
        return None
    return (
        input_tokens * prices["input"]
        + output_tokens * prices["output"]
        + cache_read_tokens * prices["cache_read"]
        + cache_write_tokens * prices["cache_write"]
    ) / 1_000_000.0


def extract_usage(result_event: dict[str, Any], *, model_hint: Optional[str] = None) -> RunUsage:
    """Pull usage counts out of a CC SDK ``result`` event.

    The SDK shape varies a little across versions; we read both the
    direct ``usage`` field and the nested ``message.usage`` form.
    Unknown fields default to 0.
    """
    usage = result_event.get("usage") or {}
    if not usage:
        msg = result_event.get("message") or {}
        usage = msg.get("usage") or {}
    model = result_event.get("model") or model_hint
    input_t = int(usage.get("input_tokens") or 0)
    output_t = int(usage.get("output_tokens") or 0)
    cache_read = int(
        usage.get("cache_read_input_tokens")
        or usage.get("cache_read_tokens")
        or 0
    )
    cache_write = int(
        usage.get("cache_creation_input_tokens")
        or usage.get("cache_write_tokens")
        or 0
    )
    cost = compute_cost(
        model=model,
        input_tokens=input_t,
        output_tokens=output_t,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
    return RunUsage(
        model=model,
        input_tokens=input_t,
        output_tokens=output_t,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost,
    )
