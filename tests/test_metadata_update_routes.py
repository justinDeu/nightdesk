"""Tests for focused ticket metadata update routes.

Covers the JSON API (/api/v1/tickets/{tid}/priority|status|project|profile)
plus the bulk variants.  Validates:
  - Sparse updates touch only the target field.
  - Status transitions respect the lifecycle state machine.
  - Invalid transitions return 409.
  - Missing tickets return 404.
  - Bulk operations return partial success with skipped details.
  - Project/profile validation rejects unknown ids.
  - Existing full PATCH semantics are preserved.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.tickets import (
    create_ticket,
    get_ticket,
    list_tickets,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        engine=engine,
        bearer_token="t",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )


@pytest.fixture
def profile(session):
    return create_profile(
        session,
        name="meta-test",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


@pytest.fixture
def profile_b(session):
    """Second profile for reassignment tests."""
    return create_profile(
        session,
        name="meta-test-b",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


async def _create_profile_api(client, name="api-test"):
    """Helper to create a profile via the JSON API and return its id."""
    r = await client.post("/api/v1/profiles", json={
        "name": name,
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
        "default_model": None,
        "claude_credentials": {"source": "inherit"},
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_ticket(session, profile, *, title="test", status="draft", priority=0,
                 project_id=None):
    return create_ticket(
        session,
        title=title,
        prompt="p",
        profile_id=profile.id,
        status=status,
        priority=priority,
        project_id=project_id,
        source_path="/tmp",
    )


# ===========================================================================
# JSON API — single-ticket focused updates
# ===========================================================================


class TestPriorityUpdate:
    """PATCH /api/v1/tickets/{tid}/priority"""

    async def test_updates_priority(self, client, session, profile):
        t = _make_ticket(session, profile, priority=0)
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/priority",
            json={"priority": 4},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["priority"] == 4
        assert body["id"] == t.id
        # Only priority changed, not title or anything else.
        assert body["title"] == "test"
        # Persisted.
        session.expire_all()
        assert get_ticket(session, t.id).priority == 4

    async def test_rejects_negative(self, client, session, profile):
        t = _make_ticket(session, profile)
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/priority",
            json={"priority": -1},
        )
        assert r.status_code == 422

    async def test_missing_ticket_404(self, client, session, profile):
        r = await client.patch(
            "/api/v1/tickets/nonexistent/priority",
            json={"priority": 3},
        )
        assert r.status_code == 404

    async def test_rejects_out_of_range(self, client, session, profile):
        t = _make_ticket(session, profile, priority=0)
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/priority",
            json={"priority": 99},
        )
        assert r.status_code == 422


class TestStatusUpdate:
    """PATCH /api/v1/tickets/{tid}/status"""

    async def test_valid_transition(self, client, session, profile):
        t = _make_ticket(session, profile, status="draft")
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/status",
            json={"status": "queued"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "queued"

    async def test_invalid_transition_returns_409(self, client, session, profile):
        t = _make_ticket(session, profile, status="draft")
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/status",
            json={"status": "review"},
        )
        assert r.status_code == 409

    async def test_full_lifecycle_transitions(self, client, session, profile):
        t = _make_ticket(session, profile, status="draft")
        # draft -> queued
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/status",
            json={"status": "queued"},
        )
        assert r.status_code == 200
        # queued -> running
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/status",
            json={"status": "running"},
        )
        assert r.status_code == 200
        # running -> review
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/status",
            json={"status": "review"},
        )
        assert r.status_code == 200
        # review -> archived
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/status",
            json={"status": "archived"},
        )
        assert r.status_code == 200

    async def test_rejects_unknown_status(self, client, session, profile):
        t = _make_ticket(session, profile, status="draft")
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/status",
            json={"status": "unknown"},
        )
        assert r.status_code == 422

    async def test_missing_ticket_404(self, client, session, profile):
        r = await client.patch(
            "/api/v1/tickets/nonexistent/status",
            json={"status": "queued"},
        )
        assert r.status_code == 404


class TestProjectUpdate:
    """PATCH /api/v1/tickets/{tid}/project"""

    async def test_assigns_project(self, client, session, profile):
        project = create_project(
            session, name="proj-a", source_path="/tmp/a",
        )
        t = _make_ticket(session, profile)
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/project",
            json={"project_id": project.id},
        )
        assert r.status_code == 200
        assert r.json()["project_id"] == project.id

    async def test_clears_project_with_null(self, client, session, profile):
        project = create_project(
            session, name="proj-b", source_path="/tmp/b",
        )
        t = _make_ticket(session, profile, project_id=project.id)
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/project",
            json={"project_id": None},
        )
        assert r.status_code == 200
        assert r.json()["project_id"] is None

    async def test_rejects_unknown_project(self, client, session, profile):
        t = _make_ticket(session, profile)
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/project",
            json={"project_id": "nonexistent"},
        )
        assert r.status_code == 404

    async def test_missing_ticket_404(self, client, session, profile):
        r = await client.patch(
            "/api/v1/tickets/nonexistent/project",
            json={"project_id": None},
        )
        assert r.status_code == 404


class TestProfileUpdate:
    """PATCH /api/v1/tickets/{tid}/profile"""

    async def test_reassigns_profile(self, client, session, profile, profile_b):
        t = _make_ticket(session, profile)
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/profile",
            json={"profile_id": profile_b.id},
        )
        assert r.status_code == 200
        assert r.json()["profile_id"] == profile_b.id

    async def test_rejects_unknown_profile(self, client, session, profile):
        t = _make_ticket(session, profile)
        r = await client.patch(
            f"/api/v1/tickets/{t.id}/profile",
            json={"profile_id": "nonexistent"},
        )
        assert r.status_code == 404

    async def test_missing_ticket_404(self, client, session, profile):
        r = await client.patch(
            "/api/v1/tickets/nonexistent/profile",
            json={"profile_id": profile.id},
        )
        assert r.status_code == 404


# ===========================================================================
# JSON API — bulk focused updates
# ===========================================================================


class TestBulkPriorityUpdate:
    """PATCH /api/v1/tickets/bulk/priority"""

    async def test_updates_multiple_tickets(self, client, session, profile):
        a = _make_ticket(session, profile, title="a", priority=0)
        b = _make_ticket(session, profile, title="b", priority=1)
        r = await client.patch(
            "/api/v1/tickets/bulk/priority",
            json={"ticket_ids": [a.id, b.id], "priority": 4},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["updated"]) == 2
        assert len(body["skipped"]) == 0
        for t in body["updated"]:
            assert t["priority"] == 4

    async def test_rejects_out_of_range_priority(self, client, session, profile):
        a = _make_ticket(session, profile, title="a")
        r = await client.patch(
            "/api/v1/tickets/bulk/priority",
            json={"ticket_ids": [a.id], "priority": 7},
        )
        assert r.status_code == 422

    async def test_skips_missing_tickets(self, client, session, profile):
        a = _make_ticket(session, profile, title="a")
        r = await client.patch(
            "/api/v1/tickets/bulk/priority",
            json={"ticket_ids": [a.id, "nonexistent"], "priority": 3},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["updated"]) == 1
        assert len(body["skipped"]) == 1
        assert body["skipped"][0]["ticket_id"] == "nonexistent"
        assert "not found" in body["skipped"][0]["reason"]

    async def test_rejects_negative_priority(self, client, session, profile):
        r = await client.patch(
            "/api/v1/tickets/bulk/priority",
            json={"ticket_ids": ["x"], "priority": -1},
        )
        assert r.status_code == 422


class TestBulkStatusUpdate:
    """PATCH /api/v1/tickets/bulk/status"""

    async def test_transitions_multiple_tickets(self, client, session, profile):
        a = _make_ticket(session, profile, title="a", status="draft")
        b = _make_ticket(session, profile, title="b", status="draft")
        r = await client.patch(
            "/api/v1/tickets/bulk/status",
            json={"ticket_ids": [a.id, b.id], "status": "queued"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["updated"]) == 2
        assert len(body["skipped"]) == 0

    async def test_skips_invalid_transitions(self, client, session, profile):
        # draft -> queued is valid, but draft -> review is not.
        a = _make_ticket(session, profile, title="a", status="draft")
        b = _make_ticket(session, profile, title="b", status="running")
        r = await client.patch(
            "/api/v1/tickets/bulk/status",
            json={"ticket_ids": [a.id, b.id], "status": "queued"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["updated"]) == 1
        assert len(body["skipped"]) == 1
        assert body["skipped"][0]["ticket_id"] == b.id

    async def test_rejects_unknown_status(self, client, session, profile):
        r = await client.patch(
            "/api/v1/tickets/bulk/status",
            json={"ticket_ids": ["x"], "status": "unknown"},
        )
        assert r.status_code == 422


class TestBulkProjectUpdate:
    """PATCH /api/v1/tickets/bulk/project"""

    async def test_assigns_project_to_multiple(self, client, session, profile):
        project = create_project(session, name="bulk-proj", source_path="/tmp/bp")
        a = _make_ticket(session, profile, title="a")
        b = _make_ticket(session, profile, title="b")
        r = await client.patch(
            "/api/v1/tickets/bulk/project",
            json={"ticket_ids": [a.id, b.id], "project_id": project.id},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["updated"]) == 2
        for t in body["updated"]:
            assert t["project_id"] == project.id

    async def test_clears_project_with_null(self, client, session, profile):
        a = _make_ticket(session, profile, title="a")
        r = await client.patch(
            "/api/v1/tickets/bulk/project",
            json={"ticket_ids": [a.id], "project_id": None},
        )
        assert r.status_code == 200
        assert r.json()["updated"][0]["project_id"] is None

    async def test_rejects_unknown_project(self, client, session, profile):
        a = _make_ticket(session, profile, title="a")
        r = await client.patch(
            "/api/v1/tickets/bulk/project",
            json={"ticket_ids": [a.id], "project_id": "nonexistent"},
        )
        assert r.status_code == 404


class TestBulkProfileUpdate:
    """PATCH /api/v1/tickets/bulk/profile"""

    async def test_reassigns_multiple_tickets(self, client, session, profile,
                                               profile_b):
        a = _make_ticket(session, profile, title="a")
        b = _make_ticket(session, profile, title="b")
        r = await client.patch(
            "/api/v1/tickets/bulk/profile",
            json={"ticket_ids": [a.id, b.id], "profile_id": profile_b.id},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["updated"]) == 2
        for t in body["updated"]:
            assert t["profile_id"] == profile_b.id

    async def test_rejects_unknown_profile(self, client, session, profile):
        a = _make_ticket(session, profile, title="a")
        r = await client.patch(
            "/api/v1/tickets/bulk/profile",
            json={"ticket_ids": [a.id], "profile_id": "nonexistent"},
        )
        assert r.status_code == 404


# ===========================================================================
# Regression: existing PATCH semantics preserved
# ===========================================================================


class TestExistingPatchPreserved:
    """Ensure the full PATCH endpoint still works alongside focused routes."""

    async def test_full_patch_still_works(self, client, session, profile):
        pid = await _create_profile_api(client, "patch-preserve")
        r = await client.post("/api/v1/tickets", json={
            "title": "full-patch", "prompt": "p",
            "profile_id": pid, "source_path": "/tmp",
        })
        assert r.status_code == 201
        tid = r.json()["id"]

        # Full PATCH with multiple fields.
        r = await client.patch(f"/api/v1/tickets/{tid}", json={
            "title": "renamed",
            "priority": 4,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["title"] == "renamed"
        assert body["priority"] == 4

    async def test_focused_route_coexists_with_patch(self, client, session,
                                                      profile):
        """Focused route and full PATCH can both modify priority independently."""
        pid = await _create_profile_api(client, "coexist")
        r = await client.post("/api/v1/tickets", json={
            "title": "coexist", "prompt": "p",
            "profile_id": pid, "source_path": "/tmp",
        })
        tid = r.json()["id"]

        # Full PATCH sets priority.
        r = await client.patch(f"/api/v1/tickets/{tid}", json={"priority": 3})
        assert r.json()["priority"] == 3

        # Focused route overrides it.
        r = await client.patch(
            f"/api/v1/tickets/{tid}/priority",
            json={"priority": 4},
        )
        assert r.json()["priority"] == 4

        # Full PATCH can still change other fields.
        r = await client.patch(f"/api/v1/tickets/{tid}", json={"title": "new"})
        assert r.json()["title"] == "new"
        assert r.json()["priority"] == 4  # untouched


# ===========================================================================
# Auth: focused routes require authentication
# ===========================================================================


class TestFocusedRoutesRequireAuth:
    """The focused JSON routes reject unauthenticated requests."""

    async def test_json_priority_requires_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.patch("/api/v1/tickets/x/priority", json={"priority": 1})
            assert r.status_code == 401

    async def test_json_status_requires_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.patch("/api/v1/tickets/x/status", json={"status": "queued"})
            assert r.status_code == 401

    async def test_json_project_requires_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.patch("/api/v1/tickets/x/project", json={"project_id": None})
            assert r.status_code == 401

    async def test_json_profile_requires_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.patch("/api/v1/tickets/x/profile", json={"profile_id": "x"})
            assert r.status_code == 401

    async def test_json_bulk_requires_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.patch("/api/v1/tickets/bulk/priority",
                               json={"ticket_ids": ["x"], "priority": 1})
            assert r.status_code == 401
