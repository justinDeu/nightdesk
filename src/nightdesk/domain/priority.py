"""Named 5-level priority scale for tickets.

Maps integer priorities to human-readable labels and colours:

    0 → No priority
    1 → Low
    2 → Medium
    3 → High
    4 → Urgent

This module is the single source of truth for priority display and parsing.
The shared property-picker ticket can replace the picker UI, but the scale
constants and helpers here should remain stable.
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Scale definition
# ---------------------------------------------------------------------------

PRIORITY_SCALE: list[dict] = [
    {"value": 0, "name": "none",    "label": "No priority", "css": "text-fg-muted border-border"},
    {"value": 1, "name": "low",     "label": "Low",          "css": "text-fg-muted border-border"},
    {"value": 2, "name": "medium",  "label": "Medium",       "css": "text-info border-info/40"},
    {"value": 3, "name": "high",    "label": "High",         "css": "text-warn border-warn/40"},
    {"value": 4, "name": "urgent",  "label": "Urgent",       "css": "text-danger border-danger/40"},
]

_VALUE_MAP: dict[int, dict] = {s["value"]: s for s in PRIORITY_SCALE}
_NAME_MAP: dict[str, dict] = {s["name"]: s for s in PRIORITY_SCALE}

# The valid numeric range.
PRIORITY_MIN = 0
PRIORITY_MAX = 4


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def priority_label(value: int) -> str:
    """Human-readable label for a priority integer. Unknown values return the
    integer as a string."""
    entry = _VALUE_MAP.get(value)
    return entry["label"] if entry else str(value)


def priority_css(value: int) -> str:
    """CSS classes for a priority chip. Returns a neutral fallback for unknown
    values."""
    entry = _VALUE_MAP.get(value)
    return entry["css"] if entry else "text-fg-muted border-border"


def priority_name(value: int) -> str:
    """Machine-friendly name for a priority integer (e.g. ``"urgent"``).
    Unknown values return ``"unknown"``."""
    entry = _VALUE_MAP.get(value)
    return entry["name"] if entry else "unknown"


def priority_from_name(name: str) -> Optional[int]:
    """Parse a named priority (case-insensitive) to its integer value.

    Returns ``None`` if *name* is not a recognised priority name. Callers
    should fall back to numeric parsing in that case.
    """
    return _NAME_MAP.get(name.lower(), {}).get("value")  # type: ignore[return-value]


def resolve_priority(value: str) -> Optional[int]:
    """Resolve a priority value that may be a name (``"urgent"``) or an
    integer string (``"3"``).

    Returns ``None`` when the value cannot be resolved.
    """
    named = priority_from_name(value)
    if named is not None:
        return named
    try:
        num = int(value)
    except (ValueError, TypeError):
        return None
    if PRIORITY_MIN <= num <= PRIORITY_MAX:
        return num
    return None


def validate_priority(value: int) -> int:
    """Return *value* when it is inside the supported priority scale."""
    if not isinstance(value, int) or not (PRIORITY_MIN <= value <= PRIORITY_MAX):
        raise ValueError(f"priority must be between {PRIORITY_MIN} and {PRIORITY_MAX}")
    return value


def is_priority_name(value: str) -> bool:
    """True when *value* is a recognised priority name (case-insensitive)."""
    return value.lower() in _NAME_MAP
