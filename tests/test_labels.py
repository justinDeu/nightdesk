"""Tests for labels CRUD API, settings page, rendering, and seed data."""
import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.labels import (
    LabelNameTaken,
    LabelNotFound,
    create_label,
    delete_label,
    get_label,
    list_labels,
    set_ticket_labels,
    update_label,
)
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket


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
        name="label-test",
        fs_read=[], fs_write=[],
        allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    )


# ---------------------------------------------------------------------------
# Domain layer tests
# ---------------------------------------------------------------------------

class TestLabelDomain:
    def test_create_label(self, session):
        label = create_label(session, name="backend", color="#3b82f6")
        assert label.name == "backend"
        assert label.color == "#3b82f6"
        assert label.id

    def test_create_label_default_color(self, session):
        label = create_label(session, name="docs")
        assert label.color == ""

    def test_create_label_duplicate_name(self, session):
        create_label(session, name="backend")
        with pytest.raises(LabelNameTaken):
            create_label(session, name="backend")

    def test_list_labels_ordered(self, session):
        create_label(session, name="zebra")
        create_label(session, name="alpha")
        create_label(session, name="middle")
        labels = list_labels(session)
        names = [l.name for l in labels]
        assert names == ["alpha", "middle", "zebra"]

    def test_get_label(self, session):
        created = create_label(session, name="infra")
        fetched = get_label(session, created.id)
        assert fetched.name == "infra"

    def test_get_label_not_found(self, session):
        with pytest.raises(LabelNotFound):
            get_label(session, "nonexistent")

    def test_update_label_name(self, session):
        label = create_label(session, name="old-name")
        updated = update_label(session, label.id, name="new-name")
        assert updated.name == "new-name"

    def test_update_label_color(self, session):
        label = create_label(session, name="test", color="#000000")
        updated = update_label(session, label.id, color="#ffffff")
        assert updated.color == "#ffffff"

    def test_update_label_duplicate_name(self, session):
        create_label(session, name="taken")
        label = create_label(session, name="other")
        with pytest.raises(LabelNameTaken):
            update_label(session, label.id, name="taken")

    def test_delete_label(self, session):
        label = create_label(session, name="to-delete")
        delete_label(session, label.id)
        with pytest.raises(LabelNotFound):
            get_label(session, label.id)

    def test_delete_label_not_found(self, session):
        with pytest.raises(LabelNotFound):
            delete_label(session, "nonexistent")

    def test_set_ticket_labels(self, session, profile):
        l1 = create_label(session, name="backend")
        l2 = create_label(session, name="ui/ux")
        ticket = create_ticket(
            session, title="test", prompt="x",
            profile_id=profile.id, source_path="/tmp",
        )
        result = set_ticket_labels(session, ticket.id, [l1.id, l2.id])
        assert len(result.labels) == 2
        names = {l.name for l in result.labels}
        assert names == {"backend", "ui/ux"}

    def test_set_ticket_labels_replace(self, session, profile):
        l1 = create_label(session, name="backend")
        l2 = create_label(session, name="docs")
        ticket = create_ticket(
            session, title="test", prompt="x",
            profile_id=profile.id, source_path="/tmp",
        )
        set_ticket_labels(session, ticket.id, [l1.id])
        result = set_ticket_labels(session, ticket.id, [l2.id])
        names = {l.name for l in result.labels}
        assert names == {"docs"}

    def test_set_ticket_labels_clear(self, session, profile):
        l1 = create_label(session, name="backend")
        ticket = create_ticket(
            session, title="test", prompt="x",
            profile_id=profile.id, source_path="/tmp",
        )
        set_ticket_labels(session, ticket.id, [l1.id])
        result = set_ticket_labels(session, ticket.id, [])
        assert len(result.labels) == 0

    def test_set_ticket_labels_invalid_id(self, session, profile):
        ticket = create_ticket(
            session, title="test", prompt="x",
            profile_id=profile.id, source_path="/tmp",
        )
        with pytest.raises(LabelNotFound):
            set_ticket_labels(session, ticket.id, ["nonexistent"])

    def test_delete_label_removes_from_ticket(self, session, profile):
        label = create_label(session, name="temp")
        ticket = create_ticket(
            session, title="test", prompt="x",
            profile_id=profile.id, source_path="/tmp",
        )
        set_ticket_labels(session, ticket.id, [label.id])
        delete_label(session, label.id)
        from nightdesk.domain.tickets import get_ticket
        refreshed = get_ticket(session, ticket.id)
        assert len(refreshed.labels) == 0

    def test_create_label_invalid_color(self, session):
        with pytest.raises(ValueError, match="invalid color"):
            create_label(session, name="bad-color", color="not-a-color")

    def test_create_label_color_no_hash(self, session):
        with pytest.raises(ValueError, match="invalid color"):
            create_label(session, name="bad-hash", color="3b82f6")

    def test_create_label_empty_color_ok(self, session):
        label = create_label(session, name="no-color", color="")
        assert label.color == ""

    def test_create_label_valid_short_hex(self, session):
        label = create_label(session, name="short-hex", color="#abc")
        assert label.color == "#abc"

    def test_update_label_invalid_color(self, session):
        label = create_label(session, name="to-update")
        with pytest.raises(ValueError, match="invalid color"):
            update_label(session, label.id, color="badcolor")


# ---------------------------------------------------------------------------
# JSON API tests
# ---------------------------------------------------------------------------

class TestLabelsAPI:
    async def test_list_labels_empty(self, cookie_client):
        r = await cookie_client.get("/api/v1/labels")
        assert r.status_code == 200
        assert r.json() == []

    async def test_create_label(self, cookie_client):
        r = await cookie_client.post("/api/v1/labels", json={
            "name": "backend", "color": "#3b82f6",
        })
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "backend"
        assert data["color"] == "#3b82f6"

    async def test_create_label_duplicate(self, cookie_client):
        await cookie_client.post("/api/v1/labels", json={"name": "dupe"})
        r = await cookie_client.post("/api/v1/labels", json={"name": "dupe"})
        assert r.status_code == 409

    async def test_get_label(self, cookie_client):
        create_r = await cookie_client.post("/api/v1/labels", json={"name": "test"})
        label_id = create_r.json()["id"]
        r = await cookie_client.get(f"/api/v1/labels/{label_id}")
        assert r.status_code == 200
        assert r.json()["name"] == "test"

    async def test_update_label(self, cookie_client):
        create_r = await cookie_client.post("/api/v1/labels", json={"name": "old"})
        label_id = create_r.json()["id"]
        r = await cookie_client.patch(f"/api/v1/labels/{label_id}", json={
            "name": "new", "color": "#ff0000",
        })
        assert r.status_code == 200
        assert r.json()["name"] == "new"
        assert r.json()["color"] == "#ff0000"

    async def test_delete_label(self, cookie_client):
        create_r = await cookie_client.post("/api/v1/labels", json={"name": "gone"})
        label_id = create_r.json()["id"]
        r = await cookie_client.delete(f"/api/v1/labels/{label_id}")
        assert r.status_code == 204

    async def test_set_ticket_labels(self, cookie_client, session, profile):
        label_r = await cookie_client.post("/api/v1/labels", json={"name": "backend"})
        label_id = label_r.json()["id"]
        ticket = create_ticket(
            session, title="label-test", prompt="x",
            profile_id=profile.id, source_path="/tmp",
        )
        r = await cookie_client.put(
            f"/api/v1/labels/tickets/{ticket.id}",
            json={"label_ids": [label_id]},
        )
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["name"] == "backend"

    async def test_create_label_invalid_color_api(self, cookie_client):
        r = await cookie_client.post("/api/v1/labels", json={
            "name": "badcolor", "color": "not-hex",
        })
        assert r.status_code == 422

    async def test_update_label_invalid_color_api(self, cookie_client):
        create_r = await cookie_client.post("/api/v1/labels", json={"name": "valid"})
        label_id = create_r.json()["id"]
        r = await cookie_client.patch(f"/api/v1/labels/{label_id}", json={
            "color": "bad",
        })
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Settings page tests
# ---------------------------------------------------------------------------

class TestLabelsSettingsPage:
    async def test_labels_settings_page_renders(self, cookie_client):
        r = await cookie_client.get("/settings/labels")
        assert r.status_code == 200
        assert "Labels" in r.text
        assert "No labels yet" in r.text

    async def test_labels_settings_page_with_labels(self, cookie_client, session):
        create_label(session, name="backend", color="#3b82f6")
        r = await cookie_client.get("/settings/labels")
        assert r.status_code == 200
        assert "backend" in r.text

    async def test_labels_settings_create(self, cookie_client):
        r = await cookie_client.post("/settings/labels", data={
            "name": "research", "color": "#f59e0b",
        }, follow_redirects=False)
        assert r.status_code == 200
        assert "research" in r.text

    async def test_labels_settings_create_duplicate(self, cookie_client, session):
        create_label(session, name="taken")
        r = await cookie_client.post("/settings/labels", data={
            "name": "taken", "color": "",
        })
        assert r.status_code == 409

    async def test_labels_settings_delete(self, cookie_client, session):
        label = create_label(session, name="to-remove")
        r = await cookie_client.post(
            f"/settings/labels/{label.id}/delete",
            follow_redirects=False,
        )
        assert r.status_code == 200
        assert "to-remove" not in r.text

    async def test_labels_settings_pane_has_edit_button(self, cookie_client, session):
        """Each label row must have an edit affordance."""
        create_label(session, name="editable", color="#3b82f6")
        r = await cookie_client.get("/settings/labels")
        assert r.status_code == 200
        assert "nd-label-edit-form" in r.text

    async def test_labels_settings_inline_update(self, cookie_client, session):
        """POST /settings/labels/{id} renames and recolors a label."""
        label = create_label(session, name="old-name", color="#000000")
        r = await cookie_client.post(
            f"/settings/labels/{label.id}",
            data={"name": "new-name", "color": "#ffffff"},
        )
        assert r.status_code == 200
        assert "new-name" in r.text
        assert "old-name" not in r.text

    async def test_labels_settings_inline_update_invalid_color(self, cookie_client, session):
        label = create_label(session, name="color-guard")
        r = await cookie_client.post(
            f"/settings/labels/{label.id}",
            data={"name": "color-guard", "color": "notacolor"},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Rendering tests
# ---------------------------------------------------------------------------

class TestLabelRendering:
    async def test_label_chips_on_ticket_card(self, cookie_client, session, profile):
        label = create_label(session, name="backend", color="#3b82f6")
        ticket = create_ticket(
            session, title="card-label-test", prompt="x",
            profile_id=profile.id, source_path="/tmp",
        )
        set_ticket_labels(session, ticket.id, [label.id])
        r = await cookie_client.get("/")
        assert r.status_code == 200
        assert "backend" in r.text

    async def test_board_card_has_inline_label_anchor(self, cookie_client, session, profile):
        """Board card label area must render with data-label-picker for inline editing."""
        ticket = create_ticket(
            session, title="anchor-test", prompt="x",
            profile_id=profile.id, source_path="/tmp",
        )
        r = await cookie_client.get("/")
        assert r.status_code == 200
        assert f'data-label-picker="{ticket.id}"' in r.text

    async def test_inline_label_picker_route(self, cookie_client, session, profile):
        """GET /board/tickets/{tid}/inline-labels returns the popover body."""
        label = create_label(session, name="infra-inline", color="#ef4444")
        ticket = create_ticket(
            session, title="inline-picker-test", prompt="x",
            profile_id=profile.id, source_path="/tmp",
        )
        set_ticket_labels(session, ticket.id, [label.id])
        r = await cookie_client.get(f"/board/tickets/{ticket.id}/inline-labels")
        assert r.status_code == 200
        assert "infra-inline" in r.text
        assert "data-inline-label-option" in r.text

    async def test_label_chips_on_sidebar(self, cookie_client, session, profile):
        label = create_label(session, name="ui/ux", color="#8b5cf6")
        ticket = create_ticket(
            session, title="sidebar-label-test", prompt="x",
            profile_id=profile.id, source_path="/tmp",
        )
        set_ticket_labels(session, ticket.id, [label.id])
        r = await cookie_client.get(f"/board/sidebar?ticket_id={ticket.id}")
        assert r.status_code == 200
        assert "ui/ux" in r.text

    async def test_sidebar_label_area_is_clickable(self, cookie_client, session, profile):
        """Sidebar labels must have ndOpenLabelPicker wiring for keyboard/click access."""
        ticket = create_ticket(
            session, title="sidebar-click-test", prompt="x",
            profile_id=profile.id, source_path="/tmp",
        )
        r = await cookie_client.get(f"/board/sidebar?ticket_id={ticket.id}")
        assert r.status_code == 200
        assert "ndOpenLabelPicker" in r.text

    async def test_label_in_ticket_detail(self, cookie_client, session, profile):
        label = create_label(session, name="infra", color="#ef4444")
        ticket = create_ticket(
            session, title="detail-label-test", prompt="x",
            profile_id=profile.id, source_path="/tmp", status="draft",
        )
        set_ticket_labels(session, ticket.id, [label.id])
        r = await cookie_client.get(f"/tickets/{ticket.id}")
        assert r.status_code == 200
        assert "infra" in r.text


# ---------------------------------------------------------------------------
# Seed data tests
# ---------------------------------------------------------------------------

class TestLabelSeedData:
    def test_label_specs_exist(self):
        from nightdesk.seed_demo import _LABEL_SPECS
        # Exact count — add a new entry here if the seeder grows.
        assert len(_LABEL_SPECS) == 8
        names = {s["name"] for s in _LABEL_SPECS}
        assert names == {"backend", "ui/ux", "bug", "research", "infra", "docs", "cleanup", "feature"}

    def test_ticket_specs_have_labels(self):
        from nightdesk.seed_demo import _TICKET_SPECS
        labeled = [s for s in _TICKET_SPECS if s.get("labels")]
        assert len(labeled) > 0
        # Every spec should have at least one label
        for spec in _TICKET_SPECS:
            assert spec.get("labels"), f"ticket '{spec['title']}' missing labels"
