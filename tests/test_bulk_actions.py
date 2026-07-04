"""Tests for selection-driven bulk ticket actions.

Covers the cookie-auth board routes that back the bulk action bar:
  - POST /board/tickets/bulk/archive    (archive draft/queued/review tickets; skip the rest)
  - POST /board/tickets/bulk/unarchive  (the archive undo path)
  - POST /board/tickets/bulk/labels     (add labels, set-union)
  - POST /board/tickets/bulk/restore    (generic undo: per-ticket prior values)
  - undo payloads returned by the existing bulk priority/status/project routes
And the board UI rendering of the bulk action bar + selection assets.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.labels import create_label, get_ticket_labels
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.tickets import create_ticket, get_ticket, transition_status


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
async def cookie_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


@pytest.fixture
def profile(session):
    return create_profile(
        session,
        name="bulk-test",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


@pytest.fixture
def profile_b(session):
    return create_profile(
        session,
        name="bulk-test-b",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


def _make_ticket(session, profile, *, title="t", status="draft", priority=0,
                 project_id=None):
    return create_ticket(
        session, title=title, prompt="p", profile_id=profile.id,
        status=status, priority=priority, project_id=project_id,
        source_path="/tmp",
    )


def _review_ticket(session, profile, *, title="rev"):
    """A ticket parked in review (one of the states from which archive is
    valid — draft/queued/review; see ``_ARCHIVABLE_SOURCES``)."""
    return _make_ticket(session, profile, title=title, status="review")


# ===========================================================================
# Bulk archive / unarchive
# ===========================================================================


class TestBulkArchive:
    """POST /board/tickets/bulk/archive"""

    async def test_archives_review_tickets(self, cookie_client, session, profile):
        a = _review_ticket(session, profile, title="a")
        b = _review_ticket(session, profile, title="b")
        r = await cookie_client.post(
            "/board/tickets/bulk/archive",
            data={"ticket_ids": f"{a.id},{b.id}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["updated"]) == 2
        assert {u["status"] for u in body["updated"]} == {"archived"}
        session.expire_all()
        assert get_ticket(session, a.id).status == "archived"
        assert get_ticket(session, b.id).status == "archived"

    async def test_archives_draft_and_queued_tickets(self, cookie_client, session, profile):
        """draft/queued are also valid archive sources — the non-destructive
        discard path for a ticket that will never run."""
        a = _make_ticket(session, profile, title="a", status="draft")
        b = _make_ticket(session, profile, title="b", status="queued")
        r = await cookie_client.post(
            "/board/tickets/bulk/archive",
            data={"ticket_ids": f"{a.id},{b.id}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["updated"]) == 2
        assert {u["status"] for u in body["updated"]} == {"archived"}
        session.expire_all()
        assert get_ticket(session, a.id).status == "archived"
        assert get_ticket(session, b.id).status == "archived"

    async def test_skips_non_archivable(self, cookie_client, session, profile):
        a = _review_ticket(session, profile, title="a")
        b = _make_ticket(session, profile, title="b", status="running")
        r = await cookie_client.post(
            "/board/tickets/bulk/archive",
            data={"ticket_ids": f"{a.id},{b.id}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["updated"]) == 1
        assert len(body["skipped"]) == 1
        assert body["skipped"][0]["ticket_id"] == b.id

    async def test_skips_missing(self, cookie_client, session, profile):
        a = _review_ticket(session, profile, title="a")
        r = await cookie_client.post(
            "/board/tickets/bulk/archive",
            data={"ticket_ids": f"{a.id},nope"},
        )
        body = r.json()
        assert len(body["updated"]) == 1
        assert body["skipped"][0] == {"ticket_id": "nope", "reason": "not found"}

    async def test_undo_payload_unarchives(self, cookie_client, session, profile):
        a = _review_ticket(session, profile, title="a")
        r = await cookie_client.post(
            "/board/tickets/bulk/archive", data={"ticket_ids": a.id},
        )
        undo = r.json()["undo"]
        assert undo["route"] == "/board/tickets/bulk/unarchive"
        assert undo["json"] is False
        assert undo["payload"]["ticket_ids"] == a.id
        # Applying the undo brings the ticket back out of archived.
        r2 = await cookie_client.post(undo["route"], data=undo["payload"])
        assert r2.status_code == 200
        assert len(r2.json()["updated"]) == 1
        session.expire_all()
        assert get_ticket(session, a.id).status == "queued"

    async def test_no_undo_when_nothing_archived(self, cookie_client, session,
                                                 profile):
        b = _make_ticket(session, profile, title="b", status="running")
        r = await cookie_client.post(
            "/board/tickets/bulk/archive", data={"ticket_ids": b.id},
        )
        assert r.json()["undo"] is None

    async def test_rejects_empty_ids(self, cookie_client):
        r = await cookie_client.post(
            "/board/tickets/bulk/archive", data={"ticket_ids": ""},
        )
        assert r.status_code == 422


class TestBulkUnarchive:
    """POST /board/tickets/bulk/unarchive"""

    async def test_unarchives_archived(self, cookie_client, session, profile):
        a = _review_ticket(session, profile, title="a")
        transition_status(session, a.id, "archived")
        r = await cookie_client.post(
            "/board/tickets/bulk/unarchive", data={"ticket_ids": a.id},
        )
        assert r.status_code == 200
        assert len(r.json()["updated"]) == 1
        session.expire_all()
        assert get_ticket(session, a.id).status == "queued"

    async def test_skips_non_archived(self, cookie_client, session, profile):
        a = _make_ticket(session, profile, title="a", status="draft")
        r = await cookie_client.post(
            "/board/tickets/bulk/unarchive", data={"ticket_ids": a.id},
        )
        assert len(r.json()["updated"]) == 0
        assert len(r.json()["skipped"]) == 1


# ===========================================================================
# Bulk labels
# ===========================================================================


class TestBulkLabels:
    """POST /board/tickets/bulk/labels"""

    async def test_adds_labels_union(self, cookie_client, session, profile):
        a = _make_ticket(session, profile, title="a")
        b = _make_ticket(session, profile, title="b")
        l1 = create_label(session, name="backend", color="#abc")
        l2 = create_label(session, name="ui", color="#def")
        # Pre-seed b with l1 so we can prove union semantics (no duplication).
        from nightdesk.domain.labels import set_ticket_labels
        set_ticket_labels(session, b.id, [l1.id])

        r = await cookie_client.post(
            "/board/tickets/bulk/labels",
            data={"ticket_ids": f"{a.id},{b.id}", "label_ids": f"{l1.id},{l2.id}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["updated"]) == 2
        session.expire_all()
        assert {lbl.id for lbl in get_ticket_labels(session, a.id)} == {l1.id, l2.id}
        # b already had l1; union keeps a single copy and adds l2.
        b_labels = [lbl.id for lbl in get_ticket_labels(session, b.id)]
        assert sorted(b_labels) == sorted([l1.id, l2.id])
        assert b_labels.count(l1.id) == 1

    async def test_undo_restores_prior_label_sets(self, cookie_client, session,
                                                  profile):
        a = _make_ticket(session, profile, title="a")
        l1 = create_label(session, name="backend")
        r = await cookie_client.post(
            "/board/tickets/bulk/labels",
            data={"ticket_ids": a.id, "label_ids": l1.id},
        )
        undo = r.json()["undo"]
        assert undo["route"] == "/board/tickets/bulk/restore"
        assert undo["json"] is True
        assert undo["payload"]["prop"] == "labels"
        assert undo["payload"]["values"][a.id] == []  # was unlabelled before
        # Apply undo -> labels cleared again.
        r2 = await cookie_client.post(undo["route"], json=undo["payload"])
        assert r2.status_code == 200
        session.expire_all()
        assert get_ticket_labels(session, a.id) == []

    async def test_creates_label_by_name_and_applies_it(self, cookie_client,
                                                       session, profile):
        a = _make_ticket(session, profile, title="a")
        b = _make_ticket(session, profile, title="b")
        r = await cookie_client.post(
            "/board/tickets/bulk/labels",
            data={
                "ticket_ids": f"{a.id},{b.id}",
                "label_name": "triage",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["label"]["name"] == "triage"
        assert body["label"]["color"] == ""
        session.expire_all()
        assert [lbl.name for lbl in get_ticket_labels(session, a.id)] == ["triage"]
        assert [lbl.name for lbl in get_ticket_labels(session, b.id)] == ["triage"]

    async def test_label_name_reuses_existing_label(self, cookie_client, session,
                                                   profile):
        a = _make_ticket(session, profile, title="a")
        existing = create_label(session, name="triage", color="#111111")
        r = await cookie_client.post(
            "/board/tickets/bulk/labels",
            data={"ticket_ids": a.id, "label_name": "triage"},
        )
        assert r.status_code == 200
        assert r.json()["label"]["id"] == existing.id
        session.expire_all()
        labels = get_ticket_labels(session, a.id)
        assert [lbl.id for lbl in labels] == [existing.id]

    async def test_removes_labels_from_selected_tickets(self, cookie_client,
                                                       session, profile):
        a = _make_ticket(session, profile, title="a")
        b = _make_ticket(session, profile, title="b")
        keep = create_label(session, name="keep")
        remove = create_label(session, name="remove")
        from nightdesk.domain.labels import set_ticket_labels
        set_ticket_labels(session, a.id, [keep.id, remove.id])
        set_ticket_labels(session, b.id, [remove.id])

        r = await cookie_client.post(
            "/board/tickets/bulk/labels",
            data={
                "ticket_ids": f"{a.id},{b.id}",
                "label_ids": remove.id,
                "label_action": "remove",
            },
        )

        assert r.status_code == 200
        session.expire_all()
        assert [lbl.id for lbl in get_ticket_labels(session, a.id)] == [keep.id]
        assert get_ticket_labels(session, b.id) == []

    async def test_unknown_label_404(self, cookie_client, session, profile):
        a = _make_ticket(session, profile, title="a")
        r = await cookie_client.post(
            "/board/tickets/bulk/labels",
            data={"ticket_ids": a.id, "label_ids": "nonexistent"},
        )
        assert r.status_code == 404

    async def test_requires_label_ids(self, cookie_client, session, profile):
        a = _make_ticket(session, profile, title="a")
        r = await cookie_client.post(
            "/board/tickets/bulk/labels",
            data={"ticket_ids": a.id, "label_ids": ""},
        )
        assert r.status_code == 422


# ===========================================================================
# Generic restore (undo) + undo payloads on existing bulk routes
# ===========================================================================


class TestBulkRestore:
    """POST /board/tickets/bulk/restore"""

    async def test_restores_priority(self, cookie_client, session, profile):
        a = _make_ticket(session, profile, title="a", priority=1)
        b = _make_ticket(session, profile, title="b", priority=2)
        r = await cookie_client.post(
            "/board/tickets/bulk/restore",
            json={"prop": "priority", "values": {a.id: 1, b.id: 2}},
        )
        assert r.status_code == 200
        assert sorted(r.json()["restored"]) == sorted([a.id, b.id])
        session.expire_all()
        assert get_ticket(session, a.id).priority == 1
        assert get_ticket(session, b.id).priority == 2

    async def test_restores_project(self, cookie_client, session, profile):
        proj = create_project(session, name="p", source_path="/tmp/p")
        a = _make_ticket(session, profile, title="a", project_id=proj.id)
        # Clear, then restore to the prior project.
        r = await cookie_client.post(
            "/board/tickets/bulk/restore",
            json={"prop": "project", "values": {a.id: ""}},
        )
        assert r.status_code == 200
        session.expire_all()
        assert get_ticket(session, a.id).project_id is None

    async def test_rejects_unknown_prop(self, cookie_client):
        r = await cookie_client.post(
            "/board/tickets/bulk/restore",
            json={"prop": "bogus", "values": {}},
        )
        assert r.status_code == 422

    async def test_status_restore_is_best_effort(self, cookie_client, session,
                                                 profile):
        # archived -> review is not a valid transition, so restore skips it
        # rather than failing the request (the "where feasible" caveat).
        a = _make_ticket(session, profile, title="a", status="review")
        transition_status(session, a.id, "archived")
        r = await cookie_client.post(
            "/board/tickets/bulk/restore",
            json={"prop": "status", "values": {a.id: "review"}},
        )
        assert r.status_code == 200
        assert a.id not in r.json()["restored"]
        assert len(r.json()["skipped"]) == 1


class TestBulkUndoPayloads:
    """The existing bulk priority/status/project routes now return undo."""

    async def test_priority_returns_restore_undo(self, cookie_client, session,
                                                 profile):
        a = _make_ticket(session, profile, title="a", priority=1)
        r = await cookie_client.post(
            "/board/tickets/bulk/priority",
            data={"ticket_ids": a.id, "priority": "4"},
        )
        undo = r.json()["undo"]
        assert undo["payload"]["prop"] == "priority"
        assert undo["payload"]["values"][a.id] == 1
        # Round-trip the undo restores the original priority.
        await cookie_client.post(undo["route"], json=undo["payload"])
        session.expire_all()
        assert get_ticket(session, a.id).priority == 1

    async def test_project_returns_restore_undo(self, cookie_client, session,
                                                profile):
        proj = create_project(session, name="p2", source_path="/tmp/p2")
        a = _make_ticket(session, profile, title="a")
        r = await cookie_client.post(
            "/board/tickets/bulk/project",
            data={"ticket_ids": a.id, "project_id": proj.id},
        )
        undo = r.json()["undo"]
        assert undo["payload"]["prop"] == "project"
        assert undo["payload"]["values"][a.id] == ""  # had no project before

    async def test_status_undo_only_for_moved(self, cookie_client, session,
                                              profile):
        a = _make_ticket(session, profile, title="a", status="draft")
        b = _make_ticket(session, profile, title="b", status="running")
        r = await cookie_client.post(
            "/board/tickets/bulk/status",
            data={"ticket_ids": f"{a.id},{b.id}", "status": "queued"},
        )
        body = r.json()
        # a moved (draft->queued); b skipped (running->queued invalid).
        assert len(body["updated"]) == 1
        undo = body["undo"]
        assert list(undo["payload"]["values"].keys()) == [a.id]
        assert undo["payload"]["values"][a.id] == "draft"


# ===========================================================================
# UI rendering — bulk action bar + selection assets
# ===========================================================================


class TestActionBarRendering:
    async def test_board_renders_action_bar(self, cookie_client, session, profile):
        proj = create_project(session, name="proj", source_path="/tmp/pp")
        create_label(session, name="backend", color="#abcdef")
        _make_ticket(session, profile, title="hello", status="review",
                     project_id=proj.id)
        r = await cookie_client.get("/")
        assert r.status_code == 200
        html = r.text
        # The bar, its menus, and the destructive archive button are present.
        assert 'id="nd-bulk-bar"' in html
        assert 'data-nd-bulk-toggle="priority"' in html
        assert 'data-nd-bulk-toggle="status"' in html
        assert 'data-nd-bulk-toggle="project"' in html
        assert 'data-nd-bulk-toggle="labels"' in html
        assert "data-nd-bulk-archive" in html
        assert "data-nd-bulk-clear" in html
        # Option sources rendered server-side (priority scale + project + label).
        assert "Urgent" in html  # priority_scale label
        assert "proj" in html
        assert "backend" in html
        # Selection model script is wired in.
        assert "bulk_select.js" in html

    async def test_action_bar_lists_all_priority_levels(self, cookie_client,
                                                        session, profile):
        _make_ticket(session, profile, title="x")
        r = await cookie_client.get("/")
        for label in ("No priority", "Low", "Medium", "High", "Urgent"):
            assert label in r.text

    async def test_list_renders_action_bar(self, cookie_client, session, profile):
        """The bulk action bar is also rendered on the /list page."""
        proj = create_project(session, name="bproj", source_path="/tmp/bp")
        create_label(session, name="blabel", color="#123456")
        _make_ticket(session, profile, title="list-bar-t", project_id=proj.id)
        r = await cookie_client.get("/list")
        assert r.status_code == 200
        html = r.text
        assert 'id="nd-bulk-bar"' in html
        assert 'data-nd-bulk-toggle="priority"' in html
        assert 'data-nd-bulk-toggle="status"' in html
        assert 'data-nd-bulk-toggle="project"' in html
        assert 'data-nd-bulk-toggle="labels"' in html
        assert 'data-nd-bulk-toggle="profile"' in html
        assert "data-nd-bulk-archive" in html
        # Option sources present.
        assert "Urgent" in html
        assert "bproj" in html
        assert "blabel" in html
        assert profile.name in html

    async def test_list_bar_has_profile_menu(self, cookie_client, session, profile):
        """The Profile menu in the bulk bar lists all profiles on /list."""
        r = await cookie_client.get("/list")
        html = r.text
        assert 'data-nd-bulk-toggle="profile"' in html
        assert f'data-nd-bulk-apply="profile"' in html
        assert profile.name in html


# ===========================================================================
# Bulk profile route
# ===========================================================================


class TestBulkProfile:
    """POST /board/tickets/bulk/profile"""

    async def test_reassigns_profile(self, cookie_client, session, profile, profile_b):
        a = _make_ticket(session, profile, title="a")
        r = await cookie_client.post(
            "/board/tickets/bulk/profile",
            data={"ticket_ids": a.id, "profile_id": profile_b.id},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["updated"]) == 1
        assert body["updated"][0]["profile_id"] == profile_b.id

    async def test_returns_undo_payload(self, cookie_client, session, profile,
                                        profile_b):
        a = _make_ticket(session, profile, title="a")
        r = await cookie_client.post(
            "/board/tickets/bulk/profile",
            data={"ticket_ids": a.id, "profile_id": profile_b.id},
        )
        undo = r.json()["undo"]
        assert undo["route"] == "/board/tickets/bulk/restore"
        assert undo["json"] is True
        assert undo["payload"]["prop"] == "profile"
        assert undo["payload"]["values"][a.id] == profile.id
        # Applying the undo restores the original profile.
        r2 = await cookie_client.post(undo["route"], json=undo["payload"])
        assert r2.status_code == 200
        from nightdesk.domain.tickets import get_ticket
        session.expire_all()
        assert get_ticket(session, a.id).profile_id == profile.id

    async def test_rejects_empty_ticket_ids(self, cookie_client, session, profile):
        r = await cookie_client.post(
            "/board/tickets/bulk/profile",
            data={"ticket_ids": "", "profile_id": profile.id},
        )
        assert r.status_code == 422

    async def test_rejects_empty_profile_id(self, cookie_client, session, profile):
        a = _make_ticket(session, profile, title="a")
        r = await cookie_client.post(
            "/board/tickets/bulk/profile",
            data={"ticket_ids": a.id, "profile_id": ""},
        )
        assert r.status_code == 422

    async def test_rejects_unknown_profile(self, cookie_client, session, profile):
        a = _make_ticket(session, profile, title="a")
        r = await cookie_client.post(
            "/board/tickets/bulk/profile",
            data={"ticket_ids": a.id, "profile_id": "no-such-profile"},
        )
        assert r.status_code == 404

    async def test_profile_in_routes_map_of_bulk_select_js(self):
        """bulk_select.js must declare a 'profile' route entry."""
        import pathlib
        js = pathlib.Path(__file__).parent.parent / "src" / "nightdesk" / "static" / "bulk_select.js"
        src = js.read_text()
        assert 'profile:' in src
        assert '/board/tickets/bulk/profile' in src
