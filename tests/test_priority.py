"""Tests for the named priority scale and priority-related routes."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

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
from nightdesk.domain.tickets import create_ticket


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

    def test_out_of_range_int_still_resolves(self):
        # Out-of-range ints are returned for numeric comparisons.
        assert resolve_priority("99") == 99

    def test_garbage_returns_none(self):
        assert resolve_priority("banana") is None

    def test_empty_returns_none(self):
        assert resolve_priority("") is None


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


# --------------------------------------------------------------------------- #
# Priority update route
# --------------------------------------------------------------------------- #

@pytest.fixture
def _profile(session):
    return create_profile(
        session, name="prio-route-test", fs_read=[], fs_write=[],
        allowed_tools=[], denied_tools=[], network_mode="off",
        network_allowlist=[], secret_keys=[], default_model=None,
    )


@pytest.fixture
def app_priority(engine, tmp_path):
    from nightdesk.api.app import create_app
    return create_app(
        engine=engine, bearer_token="t",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )


@pytest.fixture
async def prio_client(app_priority):
    transport = ASGITransport(app=app_priority)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


class TestPriorityUpdateRoute:
    async def test_update_priority_via_htmx_form(self, prio_client, session, _profile):
        t = create_ticket(session, title="prio-test", prompt="",
                          profile_id=_profile.id, status="draft",
                          source_path="/tmp", priority=0)
        r = await prio_client.post(
            f"/board/tickets/{t.id}/priority",
            data={"priority": "urgent"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200
        assert "Urgent" in r.text
        # Verify the DB was updated.
        from nightdesk.domain.tickets import get_ticket
        session.expire_all()
        updated = get_ticket(session, t.id)
        assert updated.priority == 4

    async def test_update_priority_via_json(self, prio_client, session, _profile):
        t = create_ticket(session, title="json-prio", prompt="",
                          profile_id=_profile.id, status="draft",
                          source_path="/tmp", priority=0)
        r = await prio_client.post(
            f"/board/tickets/{t.id}/priority",
            json={"priority": "high"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["priority"] == 3
        assert body["label"] == "High"

    async def test_update_priority_numeric(self, prio_client, session, _profile):
        t = create_ticket(session, title="num-prio", prompt="",
                          profile_id=_profile.id, status="draft",
                          source_path="/tmp", priority=0)
        r = await prio_client.post(
            f"/board/tickets/{t.id}/priority",
            json={"priority": "2"},
        )
        assert r.status_code == 200
        assert r.json()["priority"] == 2

    async def test_invalid_priority_returns_422(self, prio_client, session, _profile):
        t = create_ticket(session, title="bad-prio", prompt="",
                          profile_id=_profile.id, status="draft",
                          source_path="/tmp", priority=0)
        r = await prio_client.post(
            f"/board/tickets/{t.id}/priority",
            json={"priority": "banana"},
        )
        assert r.status_code == 422

    async def test_missing_ticket_returns_404(self, prio_client, session, _profile):
        r = await prio_client.post(
            "/board/tickets/nonexistent/priority",
            json={"priority": "urgent"},
        )
        assert r.status_code == 404

    async def test_update_priority_via_board_form(self, prio_client, session, _profile):
        """The ticket edit modal posts priority as a form field to the
        standard /board/tickets/{id} update endpoint."""
        t = create_ticket(session, title="form-prio", prompt="p",
                          profile_id=_profile.id, status="draft",
                          source_path="/tmp", priority=0)
        r = await prio_client.post(
            f"/board/tickets/{t.id}",
            data={
                "title": "form-prio",
                "priority": "3",
                "profile_id": _profile.id,
                "source_path": "/tmp",
            },
        )
        assert r.status_code == 200
        from nightdesk.domain.tickets import get_ticket
        session.expire_all()
        updated = get_ticket(session, t.id)
        assert updated.priority == 3

    async def test_create_ticket_with_priority(self, prio_client, session, _profile):
        """Creating a ticket via the board form includes priority."""
        r = await prio_client.post(
            "/board/tickets",
            data={
                "title": "prio-create",
                "profile_id": _profile.id,
                "source_path": "/tmp",
                "priority": "4",
            },
        )
        assert r.status_code == 204
        from nightdesk.domain.tickets import list_tickets
        tickets = list_tickets(session, status="draft")
        created = next(t for t in tickets if t.title == "prio-create")
        assert created.priority == 4


# --------------------------------------------------------------------------- #
# Board rendering of priority labels
# --------------------------------------------------------------------------- #

class TestPriorityRendering:
    async def test_card_shows_priority_label(self, prio_client, session, _profile):
        t = create_ticket(session, title="card-prio", prompt="",
                          profile_id=_profile.id, status="draft",
                          source_path="/tmp", priority=3)
        r = await prio_client.get("/")
        assert r.status_code == 200
        assert "High" in r.text

    async def test_card_hides_zero_priority(self, prio_client, session, _profile):
        t = create_ticket(session, title="card-zero", prompt="",
                          profile_id=_profile.id, status="draft",
                          source_path="/tmp", priority=0)
        r = await prio_client.get("/")
        assert r.status_code == 200
        # "No priority" should NOT appear on the card for priority=0 tickets.
        assert "No priority" not in _section_html(r.text, "draft")

    async def test_sidebar_shows_priority_picker(self, prio_client, session, _profile):
        t = create_ticket(session, title="side-prio", prompt="",
                          profile_id=_profile.id, status="draft",
                          source_path="/tmp", priority=2)
        r = await prio_client.get(f"/board/sidebar?ticket_id={t.id}")
        assert r.status_code == 200
        body = r.text
        assert "Medium" in body
        assert 'data-priority-picker' in body

    async def test_edit_modal_has_priority_select(self, prio_client, session, _profile):
        t = create_ticket(session, title="modal-prio", prompt="",
                          profile_id=_profile.id, status="draft",
                          source_path="/tmp", priority=1)
        r = await prio_client.get(f"/board/sidebar?ticket_id={t.id}")
        assert r.status_code == 200
        body = r.text
        assert 'name="priority"' in body
        assert "Low" in body
        assert "Urgent" in body
        assert "No priority" in body


def _section_html(body: str, status: str) -> str:
    """Slice out the <section id="board-col-<status>"> block."""
    marker = f'id="board-col-{status}"'
    start = body.find(marker)
    if start == -1:
        return ""
    open_tag = body.rfind("<section", 0, start)
    close = body.find("</section>", start)
    if open_tag == -1 or close == -1:
        return ""
    return body[open_tag:close + len("</section>")]
