"""Integration tests for the ux-org-foundations integration branch.

This branch merges eight UX foundation branches (priority scale, labels model +
UI, shared property picker, focused metadata routes, keyboard cursor, keyboard
property actions, command-palette shortcut hints). These tests assert the
*combined* behaviour the integration ticket calls out explicitly:

  1. The same property-picker / focused-update routes back both the mouse
     (HTMX picker) and keyboard (P/S shortcut) flows — one write path per
     property, no duplication.
  2. Labels render on board cards and the ticket detail page, and are
     queryable through the search grammar (``label:...``) and value suggestions.
  3. The H/J/K/L board-cursor spine is wired (column navigation + Shift+L for
     labels) and survives board polling via cursor restoration.
  4. The single-value property picker and the multi-select label picker are
     distinct primitives (separate JS files, disjoint window globals), not a
     duplicated picker.

Server-rendering + route integration level. Full keyboard-event dispatch needs
a browser harness and is covered by manual validation; here we assert the JS
and templates ship the required wiring.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.labels import create_label, set_ticket_labels
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.query import parse_query, search_tickets, suggest_values
from nightdesk.domain.tickets import create_ticket, get_ticket

_SRC_STATIC = Path(__file__).resolve().parent.parent / "src" / "nightdesk" / "static"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(engine, tmp_path):
    static_dst = tmp_path / "static"
    if _SRC_STATIC.is_dir():
        shutil.copytree(str(_SRC_STATIC), str(static_dst), dirs_exist_ok=True)
    return create_app(
        engine=engine,
        bearer_token="t",
        static_root=static_dst,
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )


@pytest.fixture
async def cookie_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


@pytest.fixture
def profile(session):
    return create_profile(
        session, name="default", fs_read=["/tmp"], fs_write=["/tmp"],
        allowed_tools=["Read"], denied_tools=[], network_mode="off",
        network_allowlist=[], secret_keys=[],
    )


def _make_ticket(session, profile, **kw):
    return create_ticket(
        session, title=kw.pop("title", "demo"), prompt="p",
        status=kw.pop("status", "draft"), profile_id=profile.id,
        source_path="/tmp", **kw,
    )


# ---------------------------------------------------------------------------
# 1. Mouse (picker) and keyboard (shortcut) share one write path per property
# ---------------------------------------------------------------------------


class TestSharedPropertyRoutes:
    async def test_picker_route_and_focused_route_hit_same_priority_path(
        self, cookie_client, session, profile
    ):
        """The shared picker posts to /property/{prop}; the focused alias posts
        to /priority. Both must land the same value through the same registry."""
        t = _make_ticket(session, profile, priority=0)

        # Mouse flow: the anchored property picker commits via /property/{prop}.
        r1 = await cookie_client.post(
            f"/board/tickets/{t.id}/property/priority", data={"value": "high"}
        )
        assert r1.status_code == 200
        session.expire_all()
        assert get_ticket(session, t.id).priority == 3

        # Keyboard/back-compat flow: the focused /priority alias.
        r2 = await cookie_client.post(
            f"/board/tickets/{t.id}/priority", data={"priority": "1"}
        )
        assert r2.status_code == 200
        session.expire_all()
        assert get_ticket(session, t.id).priority == 1

    async def test_status_picker_and_focused_route_agree(
        self, cookie_client, session, profile
    ):
        t = _make_ticket(session, profile, status="draft")
        # Picker route.
        r1 = await cookie_client.post(
            f"/board/tickets/{t.id}/property/status", data={"value": "queued"}
        )
        assert r1.status_code == 200
        session.expire_all()
        assert get_ticket(session, t.id).status == "queued"

        # Focused alias enforces the same lifecycle state machine (409 on an
        # illegal jump), proving it is not a divergent code path.
        r2 = await cookie_client.post(
            f"/board/tickets/{t.id}/status", data={"new_status": "review"}
        )
        assert r2.status_code == 409

    async def test_property_menu_route_serves_picker_options(
        self, cookie_client, session, profile
    ):
        """The keyboard P shortcut and the mouse click both open the same
        lazy-loaded picker menu route."""
        t = _make_ticket(session, profile)
        r = await cookie_client.get(f"/board/tickets/{t.id}/picker/priority")
        assert r.status_code == 200
        # Menu offers the named priority levels.
        assert "Urgent" in r.text and "Medium" in r.text


# ---------------------------------------------------------------------------
# 2. Labels render on cards/rows and are queryable
# ---------------------------------------------------------------------------


class TestLabelsRenderAndQuery:
    async def test_labels_render_on_board_card_and_detail(
        self, cookie_client, session, profile
    ):
        t = _make_ticket(session, profile, title="labelled")
        backend = create_label(session, name="backend", color="#3366ff")
        set_ticket_labels(session, t.id, [backend.id])
        session.commit()

        board = await cookie_client.get("/")
        assert board.status_code == 200
        assert "backend" in board.text

        detail = await cookie_client.get(f"/tickets/{t.id}")
        assert detail.status_code == 200
        assert "backend" in detail.text

    async def test_labels_are_queryable(self, session, profile):
        a = _make_ticket(session, profile, title="a")
        b = _make_ticket(session, profile, title="b")
        backend = create_label(session, name="backend")
        docs = create_label(session, name="docs")
        set_ticket_labels(session, a.id, [backend.id])
        set_ticket_labels(session, b.id, [docs.id])
        session.commit()

        hits = {x.id for x in search_tickets(session, parse_query("label:backend"))}
        assert a.id in hits and b.id not in hits

        # A second label partitions the board independently.
        docs_hits = {x.id for x in search_tickets(session, parse_query("label:docs"))}
        assert b.id in docs_hits and a.id not in docs_hits

        none = list(search_tickets(session, parse_query("label:nonexistent")))
        assert none == []

    async def test_label_value_suggestions(self, session, profile):
        create_label(session, name="infra")
        create_label(session, name="cleanup")
        session.commit()
        suggestions = suggest_values(session, field="label")
        values = {s["value"] if isinstance(s, dict) else s for s in suggestions}
        assert "infra" in values and "cleanup" in values


# ---------------------------------------------------------------------------
# 3. H/J/K/L cursor spine is wired and survives polling
# ---------------------------------------------------------------------------


class TestKeyboardSpine:
    async def test_hjkl_navigation_wired_in_js(self, cookie_client):
        r = await cookie_client.get("/static/command_palette.js")
        assert r.status_code == 200
        js = r.text
        # Vertical cursor (J/K) and horizontal column jumps (H/L).
        assert "moveCursor" in js
        assert "moveColumn" in js
        assert '"h"' in js or "'h'" in js
        # Plain "l" is column navigation; labels moved to Shift+L.
        assert 'key === "l" && !e.shiftKey' in js
        assert 'key === "L" && e.shiftKey' in js

    async def test_cursor_survives_board_polling(self, cookie_client):
        """Board columns are OOB-swapped by the 3s poll; the cursor must be
        re-applied afterwards so H/J/K/L navigation is not lost on refresh."""
        r = await cookie_client.get("/static/command_palette.js")
        js = r.text
        assert "restoreCursor" in js
        assert "htmx:oobAfterSwap" in js

    async def test_cheatsheet_documents_column_nav_and_shift_l(self, cookie_client):
        r = await cookie_client.get("/profiles")
        assert r.status_code == 200
        # Column navigation is documented alongside J/K.
        assert "Previous / next column" in r.text
        assert "Next / previous ticket" in r.text
        # Labels are still documented, now on Shift+L.
        assert "Set labels" in r.text

    async def test_cursor_highlight_css_present(self, cookie_client):
        r = await cookie_client.get("/static/app.css")
        assert r.status_code == 200
        assert "nd-cursor-active" in r.text


# ---------------------------------------------------------------------------
# 4. Picker primitives are distinct, not duplicated
# ---------------------------------------------------------------------------


class TestPickerPrimitivesNotDuplicated:
    async def test_single_value_property_picker_entrypoint(self, cookie_client):
        r = await cookie_client.get("/static/property_picker.js")
        assert r.status_code == 200
        assert "window.ndOpenPropertyPicker" in r.text
        # The single-value picker must NOT define the multi-select label API.
        assert "window.ndPickerAdd" not in r.text

    async def test_label_picker_is_separate_primitive(self, cookie_client):
        r = await cookie_client.get("/static/label_picker.js")
        assert r.status_code == 200
        assert "window.ndPickerAdd" in r.text
        # ...and must NOT redefine the single-value picker entrypoint.
        assert "window.ndOpenPropertyPicker =" not in r.text

    async def test_base_html_loads_both_pickers(self, cookie_client):
        r = await cookie_client.get("/")
        assert r.status_code == 200
        assert "property_picker.js" in r.text
        assert "label_picker.js" in r.text
