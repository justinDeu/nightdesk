"""Tests for the named priority scale and priority-related domain functions.

The board rendering / HTMX update route tests were removed with the HTMX
rip-out; the focused JSON priority endpoint has its own coverage in
test_metadata_update_routes.py.
"""
from __future__ import annotations

import pytest

from nightdesk.domain.priority import (
    PRIORITY_SCALE,
    PRIORITY_MIN,
    PRIORITY_MAX,
    is_priority_name,
    priority_css,
    priority_from_name,
    priority_label,
    priority_name,
    resolve_priority,
)
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket, update_ticket_priority


# --------------------------------------------------------------------------- #
# Priority scale constants
# --------------------------------------------------------------------------- #

class TestPriorityScale:
    def test_scale_has_five_levels(self):
        assert len(PRIORITY_SCALE) == 5

    def test_values_are_zero_through_four(self):
        assert [s["value"] for s in PRIORITY_SCALE] == [0, 1, 2, 3, 4]

    def test_labels(self):
        labels = {s["value"]: s["label"] for s in PRIORITY_SCALE}
        assert labels == {
            0: "No priority",
            1: "Low",
            2: "Medium",
            3: "High",
            4: "Urgent",
        }

    def test_names(self):
        names = {s["value"]: s["name"] for s in PRIORITY_SCALE}
        assert names == {
            0: "none",
            1: "low",
            2: "medium",
            3: "high",
            4: "urgent",
        }

    def test_min_max(self):
        assert PRIORITY_MIN == 0
        assert PRIORITY_MAX == 4


class TestPriorityLabel:
    @pytest.mark.parametrize("value,expected", [
        (0, "No priority"),
        (1, "Low"),
        (2, "Medium"),
        (3, "High"),
        (4, "Urgent"),
    ])
    def test_known_values(self, value, expected):
        assert priority_label(value) == expected

    def test_unknown_returns_string(self):
        assert priority_label(99) == "99"


class TestPriorityCss:
    def test_zero_is_muted(self):
        assert "text-fg-muted" in priority_css(0)

    def test_urgent_has_danger(self):
        assert "text-danger" in priority_css(4)

    def test_unknown_falls_back(self):
        assert "text-fg-muted" in priority_css(99)


class TestPriorityName:
    @pytest.mark.parametrize("value,expected", [
        (0, "none"), (1, "low"), (2, "medium"), (3, "high"), (4, "urgent"),
    ])
    def test_known(self, value, expected):
        assert priority_name(value) == expected

    def test_unknown(self):
        assert priority_name(99) == "unknown"


class TestPriorityFromName:
    @pytest.mark.parametrize("name,expected", [
        ("urgent", 4), ("high", 3), ("medium", 2), ("low", 1), ("none", 0),
        ("Urgent", 4), ("HIGH", 3), ("Medium", 2),
    ])
    def test_known(self, name, expected):
        assert priority_from_name(name) == expected

    def test_unknown_returns_none(self):
        assert priority_from_name("critical") is None


class TestResolvePriority:
    @pytest.mark.parametrize("value,expected", [
        ("urgent", 4), ("0", 0), ("3", 3), ("medium", 2),
    ])
    def test_valid(self, value, expected):
        assert resolve_priority(value) == expected

    def test_out_of_range_int_returns_none(self):
        assert resolve_priority("99") is None

    def test_negative_int_returns_none(self):
        assert resolve_priority("-1") is None

    def test_garbage_returns_none(self):
        assert resolve_priority("banana") is None

    def test_empty_returns_none(self):
        assert resolve_priority("") is None


class TestPriorityValidation:
    @pytest.mark.parametrize("value", [-1, 5, 99])
    def test_create_ticket_rejects_out_of_range_priority(self, session, _profile, value):
        with pytest.raises(ValueError, match="priority must be between 0 and 4"):
            create_ticket(session, title="bad", prompt="", profile_id=_profile.id,
                          status="draft", source_path="/tmp", priority=value)

    @pytest.mark.parametrize("value", [-1, 5, 99])
    def test_update_ticket_priority_rejects_out_of_range_priority(self, session, _profile, value):
        t = create_ticket(session, title="ok", prompt="", profile_id=_profile.id,
                          status="draft", source_path="/tmp", priority=0)
        with pytest.raises(ValueError, match="priority must be between 0 and 4"):
            update_ticket_priority(session, t.id, value)


class TestIsPriorityName:
    def test_known_names(self):
        for s in PRIORITY_SCALE:
            assert is_priority_name(s["name"]) is True

    def test_case_insensitive(self):
        assert is_priority_name("URGENT") is True
        assert is_priority_name("Low") is True

    def test_unknown(self):
        assert is_priority_name("critical") is False


# --------------------------------------------------------------------------- #
# Named priority query parsing
# --------------------------------------------------------------------------- #

class TestPriorityQueryParsing:
    """Test that priority=urgent and priority>=3 both work in the query parser."""

    def _profile(self, session, name="q-test"):
        return create_profile(
            session, name=name, fs_read=[], fs_write=[], allowed_tools=[],
            denied_tools=[], network_mode="off", network_allowlist=[],
            secret_keys=[], default_model=None,
        )

    def test_priority_named_urgent(self, session):
        from nightdesk.domain.query import parse_query, search_tickets
        p = self._profile(session)
        t1 = create_ticket(session, title="urgent-task", prompt="",
                           profile_id=p.id, status="draft", source_path="/tmp",
                           priority=4)
        t2 = create_ticket(session, title="chill-task", prompt="",
                           profile_id=p.id, status="draft", source_path="/tmp",
                           priority=0)
        got = search_tickets(session, parse_query("priority=urgent"))
        ids = [t.id for t in got]
        assert t1.id in ids
        assert t2.id not in ids

    def test_priority_named_low(self, session):
        from nightdesk.domain.query import parse_query, search_tickets
        p = self._profile(session)
        t1 = create_ticket(session, title="low-task", prompt="",
                           profile_id=p.id, status="draft", source_path="/tmp",
                           priority=1)
        got = search_tickets(session, parse_query("priority=low"))
        ids = [t.id for t in got]
        assert t1.id in ids

    def test_priority_numeric_gte(self, session):
        from nightdesk.domain.query import parse_query, search_tickets
        p = self._profile(session)
        t_low = create_ticket(session, title="low", prompt="",
                              profile_id=p.id, status="draft", source_path="/tmp",
                              priority=1)
        t_high = create_ticket(session, title="high", prompt="",
                               profile_id=p.id, status="draft", source_path="/tmp",
                               priority=3)
        got = search_tickets(session, parse_query("priority>=3"))
        ids = [t.id for t in got]
        assert t_high.id in ids
        assert t_low.id not in ids

    def test_priority_named_not_equals(self, session):
        from nightdesk.domain.query import parse_query, search_tickets
        p = self._profile(session)
        t_urg = create_ticket(session, title="urg", prompt="",
                              profile_id=p.id, status="draft", source_path="/tmp",
                              priority=4)
        t_low = create_ticket(session, title="low", prompt="",
                              profile_id=p.id, status="draft", source_path="/tmp",
                              priority=1)
        got = search_tickets(session, parse_query("priority!=urgent"))
        ids = [t.id for t in got]
        assert t_low.id in ids
        assert t_urg.id not in ids

    def test_priority_named_medium(self, session):
        from nightdesk.domain.query import parse_query, search_tickets
        p = self._profile(session)
        t = create_ticket(session, title="med", prompt="",
                          profile_id=p.id, status="draft", source_path="/tmp",
                          priority=2)
        got = search_tickets(session, parse_query("priority=medium"))
        ids = [t.id for t in got]
        assert t.id in ids


@pytest.fixture
def _profile(session):
    return create_profile(
        session, name="prio-route-test", fs_read=[], fs_write=[],
        allowed_tools=[], denied_tools=[], network_mode="off",
        network_allowlist=[], secret_keys=[], default_model=None,
    )
