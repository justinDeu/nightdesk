"""Tests for the shared property-picker primitive.

Covers the domain registry (domain.properties) and the two generic routes that
back the one reusable picker — GET /board/tickets/{id}/picker/{prop} (menu) and
POST /board/tickets/{id}/property/{prop} (focused commit) — plus the template
wiring on cards, the sidebar, and the ticket-detail header. Priority's own
back-compat route has dedicated coverage in test_priority.py; here we assert
priority, status, and project all flow through the SAME shared machinery.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.properties import (
    PROPERTY_REGISTRY,
    get_property,
    property_chip,
    status_targets,
)
from nightdesk.domain.tickets import create_ticket, get_ticket


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def _profile(session):
    return create_profile(
        session, name="prop-test", fs_read=[], fs_write=[],
        allowed_tools=[], denied_tools=[], network_mode="off",
        network_allowlist=[], secret_keys=[], default_model=None,
    )


@pytest.fixture
def _project(session):
    return create_project(
        session, name="Nightdesk", color="#34d399", source_path="/tmp/nd",
    )


@pytest.fixture
def app_prop(engine, tmp_path):
    return create_app(
        engine=engine, bearer_token="t",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )


@pytest.fixture
async def client(app_prop):
    transport = ASGITransport(app=app_prop)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


def _make(session, profile, **kw):
    kw.setdefault("title", "t")
    kw.setdefault("prompt", "")
    kw.setdefault("source_path", "/tmp")
    return create_ticket(session, profile_id=profile.id, **kw)


def _reload(session, tid):
    """Re-read a ticket after a route committed it through a different session
    (the in-memory DB is shared via one connection; we just need to drop the
    test session's identity-map cache first)."""
    session.expire_all()
    return get_ticket(session, tid)


# --------------------------------------------------------------------------- #
# Registry / domain layer
# --------------------------------------------------------------------------- #

class TestRegistry:
    def test_registers_core_properties(self):
        assert set(PROPERTY_REGISTRY) >= {"priority", "status", "project"}

    def test_unknown_property_raises(self):
        with pytest.raises(KeyError):
            get_property("nope")

    def test_only_project_is_searchable(self):
        assert get_property("project").searchable is True
        assert get_property("priority").searchable is False
        assert get_property("status").searchable is False


class TestStatusTargets:
    def test_draft_offers_queued_not_running(self):
        # Inbox promotion: draft -> queued must be offered; running is the
        # worker's job and is never a picker target.
        targets = status_targets("draft")
        assert "queued" in targets
        assert "running" not in targets
        assert targets[0] == "draft"  # current first

    def test_review_offers_queue_and_archive(self):
        assert set(status_targets("review")) == {"review", "queued", "archived"}

    def test_archived_offers_requeue(self):
        assert "queued" in status_targets("archived")


class TestChips:
    def test_priority_chip(self, session, _profile):
        t = _make(session, _profile, priority=4)
        chip = property_chip("priority", t)
        assert chip["label"] == "Urgent"
        assert "text-danger" in chip["css"]

    def test_status_chip(self, session, _profile):
        t = _make(session, _profile, status="review")
        chip = property_chip("status", t)
        assert chip["label"] == "Review"

    def test_project_chip_none(self, session, _profile):
        t = _make(session, _profile)
        chip = property_chip("project", t)
        assert chip["label"] == "No project"
        assert chip["swatch_color"] is None

    def test_project_chip_with_project(self, session, _profile, _project):
        t = _make(session, _profile, project_id=_project.id)
        chip = property_chip("project", t, _project)
        assert chip["label"] == "Nightdesk"
        assert chip["swatch_color"] == "#34d399"


class TestApply:
    def test_priority_apply_named(self, session, _profile):
        t = _make(session, _profile, priority=0)
        get_property("priority").apply(session, t.id, "urgent")
        assert get_ticket(session, t.id).priority == 4

    def test_priority_apply_invalid_raises(self, session, _profile):
        t = _make(session, _profile)
        with pytest.raises(ValueError):
            get_property("priority").apply(session, t.id, "banana")

    def test_status_apply_promote(self, session, _profile):
        t = _make(session, _profile, status="draft")
        get_property("status").apply(session, t.id, "queued")
        assert _reload(session, t.id).status == "queued"

    def test_status_apply_invalid_value(self, session, _profile):
        t = _make(session, _profile, status="draft")
        with pytest.raises(ValueError):
            get_property("status").apply(session, t.id, "bogus")

    def test_status_apply_running_rejected(self, session, _profile):
        t = _make(session, _profile, status="draft")
        with pytest.raises(ValueError):
            get_property("status").apply(session, t.id, "running")

    def test_status_apply_same_is_noop(self, session, _profile):
        t = _make(session, _profile, status="draft")
        out = get_property("status").apply(session, t.id, "draft")
        assert out.status == "draft"

    def test_project_apply_and_clear(self, session, _profile, _project):
        t = _make(session, _profile)
        get_property("project").apply(session, t.id, _project.id)
        assert get_ticket(session, t.id).project_id == _project.id
        get_property("project").apply(session, t.id, "")
        assert _reload(session, t.id).project_id is None


# --------------------------------------------------------------------------- #
# Menu route: GET /board/tickets/{id}/picker/{prop}
# --------------------------------------------------------------------------- #

class TestMenuRoute:
    async def test_priority_menu(self, client, session, _profile):
        t = _make(session, _profile, priority=2)
        r = await client.get(f"/board/tickets/{t.id}/picker/priority")
        assert r.status_code == 200
        assert "Urgent" in r.text and "No priority" in r.text
        assert "data-property-option" in r.text
        # The current value is flagged.
        assert "aria-selected" in r.text

    async def test_status_menu_lists_valid_targets(self, client, session, _profile):
        t = _make(session, _profile, status="draft")
        r = await client.get(f"/board/tickets/{t.id}/picker/status")
        assert r.status_code == 200
        assert "Queued" in r.text
        # running is never an offered target
        assert ">Running<" not in r.text

    async def test_project_menu_is_searchable(self, client, session, _profile, _project):
        t = _make(session, _profile)
        r = await client.get(f"/board/tickets/{t.id}/picker/project")
        assert r.status_code == 200
        assert "data-property-search" in r.text
        assert "No project" in r.text
        assert "Nightdesk" in r.text

    async def test_unknown_property_404(self, client, session, _profile):
        t = _make(session, _profile)
        r = await client.get(f"/board/tickets/{t.id}/picker/bogus")
        assert r.status_code == 404

    async def test_missing_ticket_404(self, client):
        r = await client.get("/board/tickets/nope/picker/priority")
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Commit route: POST /board/tickets/{id}/property/{prop}
# --------------------------------------------------------------------------- #

class TestCommitRoute:
    async def test_priority_htmx_returns_chip(self, client, session, _profile):
        t = _make(session, _profile, priority=0)
        r = await client.post(
            f"/board/tickets/{t.id}/property/priority",
            data={"value": "high"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200
        assert "High" in r.text
        assert f'data-property-chip="{t.id}:priority"' in r.text
        assert _reload(session, t.id).priority == 3

    async def test_priority_json(self, client, session, _profile):
        t = _make(session, _profile, priority=0)
        r = await client.post(
            f"/board/tickets/{t.id}/property/priority",
            json={"value": "2"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["property"] == "priority"
        assert body["label"] == "Medium"

    async def test_status_promote_via_picker(self, client, session, _profile):
        t = _make(session, _profile, status="draft")
        r = await client.post(
            f"/board/tickets/{t.id}/property/status",
            data={"value": "queued"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200
        assert _reload(session, t.id).status == "queued"

    async def test_status_invalid_transition_409(self, client, session, _profile):
        t = _make(session, _profile, status="draft")
        # draft -> archived is not a valid transition.
        r = await client.post(
            f"/board/tickets/{t.id}/property/status",
            data={"value": "archived"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 409

    async def test_status_bad_value_422(self, client, session, _profile):
        t = _make(session, _profile, status="draft")
        r = await client.post(
            f"/board/tickets/{t.id}/property/status",
            data={"value": "bogus"},
        )
        assert r.status_code == 422

    async def test_project_assign_and_clear(self, client, session, _profile, _project):
        t = _make(session, _profile)
        r = await client.post(
            f"/board/tickets/{t.id}/property/project",
            data={"value": _project.id},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200
        assert "Nightdesk" in r.text
        assert _reload(session, t.id).project_id == _project.id
        # Clear it.
        r2 = await client.post(
            f"/board/tickets/{t.id}/property/project",
            data={"value": ""},
            headers={"HX-Request": "true"},
        )
        assert r2.status_code == 200
        assert _reload(session, t.id).project_id is None

    async def test_project_unknown_404(self, client, session, _profile):
        t = _make(session, _profile)
        r = await client.post(
            f"/board/tickets/{t.id}/property/project",
            data={"value": "does-not-exist"},
        )
        assert r.status_code == 404

    async def test_unknown_property_404(self, client, session, _profile):
        t = _make(session, _profile)
        r = await client.post(
            f"/board/tickets/{t.id}/property/bogus",
            data={"value": "x"},
        )
        assert r.status_code == 404

    async def test_missing_ticket_404(self, client):
        r = await client.post(
            "/board/tickets/nope/property/priority",
            data={"value": "1"},
        )
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Template wiring — pickers reachable from card, sidebar, detail
# --------------------------------------------------------------------------- #

class TestSurfaces:
    async def test_card_uses_shared_picker(self, client, session, _profile, _project):
        t = _make(session, _profile, status="draft", priority=3,
                  project_id=_project.id)
        r = await client.get("/")
        assert r.status_code == 200
        body = r.text
        assert f'data-property-picker="{t.id}:priority"' in body
        assert f'data-property-picker="{t.id}:project"' in body

    async def test_sidebar_has_status_priority_project(self, client, session, _profile):
        t = _make(session, _profile, status="draft", priority=2)
        r = await client.get(f"/board/sidebar?ticket_id={t.id}")
        assert r.status_code == 200
        body = r.text
        for prop in ("status", "priority", "project"):
            assert f'data-property-picker="{t.id}:{prop}"' in body

    async def test_detail_header_has_pickers(self, client, session, _profile):
        t = _make(session, _profile, status="review", priority=1)
        r = await client.get(f"/tickets/{t.id}")
        assert r.status_code == 200
        body = r.text
        for prop in ("status", "priority", "project"):
            assert f'data-property-picker="{t.id}:{prop}"' in body

    async def test_property_picker_js_loaded(self, client, session, _profile):
        t = _make(session, _profile)
        r = await client.get("/")
        assert "property_picker.js" in r.text
        # The one-off priority picker script is gone.
        assert "priority_picker.js" not in r.text
