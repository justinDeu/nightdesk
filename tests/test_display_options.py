"""Tests for the display options domain helpers: normalize_props."""
from __future__ import annotations

from nightdesk.domain.display import (
    BOARD_ORDER_OPTIONS,
    CARD_PROPERTY_OPTIONS,
    LIST_PROPERTY_OPTIONS,
    normalize_props,
)

_ALL_LIST_KEYS = frozenset(k for k, _ in LIST_PROPERTY_OPTIONS)
_ALL_CARD_KEYS = frozenset(k for k, _ in CARD_PROPERTY_OPTIONS)


def test_list_property_options_has_expected_keys():
    keys = {k for k, _ in LIST_PROPERTY_OPTIONS}
    assert keys == {"priority", "project", "labels", "profile", "last_run", "updated"}


def test_card_property_options_has_expected_keys():
    keys = {k for k, _ in CARD_PROPERTY_OPTIONS}
    assert keys == {"priority", "project", "labels", "profile"}


def test_board_order_options_non_empty():
    assert len(BOARD_ORDER_OPTIONS) >= 3
    keys = {k for k, _ in BOARD_ORDER_OPTIONS}
    assert "manual" in keys
    assert "priority" in keys


def test_normalize_props_none_returns_all_list():
    result = normalize_props(None, _ALL_LIST_KEYS)
    assert result == _ALL_LIST_KEYS


def test_normalize_props_empty_string_returns_all_list():
    result = normalize_props("", _ALL_LIST_KEYS)
    assert result == _ALL_LIST_KEYS


def test_normalize_props_subset():
    result = normalize_props("priority,labels", _ALL_LIST_KEYS)
    assert result == frozenset({"priority", "labels"})


def test_normalize_props_invalid_keys_stripped():
    result = normalize_props("priority,bogus,labels", _ALL_LIST_KEYS)
    assert result == frozenset({"priority", "labels"})


def test_normalize_props_all_invalid_returns_all():
    result = normalize_props("bogus,unknown", _ALL_LIST_KEYS)
    assert result == _ALL_LIST_KEYS


def test_normalize_props_whitespace_trimmed():
    result = normalize_props("  priority , labels ", _ALL_LIST_KEYS)
    assert result == frozenset({"priority", "labels"})


def test_normalize_props_card_subset():
    result = normalize_props("priority,profile", _ALL_CARD_KEYS)
    assert result == frozenset({"priority", "profile"})
