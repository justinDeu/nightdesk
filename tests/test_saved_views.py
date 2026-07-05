"""Tests for Saved Views.

Covers:
  - Domain CRUD: create, list, get, rename, delete, reorder
  - Domain validation: name required, surface whitelist, params whitelist
  - URL composition: q/group/order round-trip for board and list
  - JSON API: full CRUD + reorder on /api/v1/views (auth + behavior)
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.saved_views import (
    SURFACE_ALLOWED_PARAMS,
    VALID_SURFACES,
    SavedViewNameTaken,
    SavedViewNotFound,
    create_saved_view,
    delete_saved_view,
    get_saved_view,
    list_saved_views,
    rename_saved_view,
    reorder_saved_views,
    view_url,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        engine=engine,
        bearer_token="t",
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


@pytest.fixture
async def bearer_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": "Bearer t"},
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Domain: CRUD basics
# ---------------------------------------------------------------------------


def test_create_and_list(session):
    v = create_saved_view(session, name="My view", surface="board", params={"q": "test"})
    assert v.id
    assert v.name == "My view"
    assert v.surface == "board"
    assert v.params == {"q": "test"}

    views = list_saved_views(session)
    assert len(views) == 1
    assert views[0].id == v.id


def test_list_empty(session):
    assert list_saved_views(session) == []


def test_get_found(session):
    v = create_saved_view(session, name="A", surface="list", params={})
    fetched = get_saved_view(session, v.id)
    assert fetched.id == v.id


def test_get_not_found(session):
    with pytest.raises(SavedViewNotFound):
        get_saved_view(session, "nonexistent")


def test_rename(session):
    v = create_saved_view(session, name="Old name", surface="board", params={})
    renamed = rename_saved_view(session, v.id, name="New name")
    assert renamed.name == "New name"
    assert get_saved_view(session, v.id).name == "New name"


def test_rename_no_op_same_name(session):
    v = create_saved_view(session, name="Same", surface="board", params={})
    result = rename_saved_view(session, v.id, name="Same")
    assert result.name == "Same"


def test_rename_conflict(session):
    create_saved_view(session, name="Alpha", surface="board", params={})
    v2 = create_saved_view(session, name="Beta", surface="board", params={})
    with pytest.raises(SavedViewNameTaken):
        rename_saved_view(session, v2.id, name="Alpha")


def test_rename_not_found(session):
    with pytest.raises(SavedViewNotFound):
        rename_saved_view(session, "bad-id", name="Anything")


def test_delete(session):
    v = create_saved_view(session, name="To delete", surface="board", params={})
    delete_saved_view(session, v.id)
    assert list_saved_views(session) == []


def test_delete_not_found(session):
    with pytest.raises(SavedViewNotFound):
        delete_saved_view(session, "bad-id")


def test_create_duplicate_name_raises(session):
    create_saved_view(session, name="Dup", surface="board", params={})
    with pytest.raises(SavedViewNameTaken):
        create_saved_view(session, name="Dup", surface="list", params={})


def test_position_auto_increments(session):
    v1 = create_saved_view(session, name="First", surface="board", params={})
    v2 = create_saved_view(session, name="Second", surface="list", params={})
    assert v2.position > v1.position


def test_list_ordered_by_position(session):
    v1 = create_saved_view(session, name="A", surface="board", params={})
    v2 = create_saved_view(session, name="B", surface="list", params={})
    views = list_saved_views(session)
    assert [v.id for v in views] == [v1.id, v2.id]


def test_reorder(session):
    v1 = create_saved_view(session, name="First", surface="board", params={})
    v2 = create_saved_view(session, name="Second", surface="board", params={})
    reorder_saved_views(session, [v2.id, v1.id])
    views = list_saved_views(session)
    assert views[0].id == v2.id
    assert views[1].id == v1.id


# ---------------------------------------------------------------------------
# Domain: validation
# ---------------------------------------------------------------------------


def test_name_required(session):
    with pytest.raises(ValueError, match="name is required"):
        create_saved_view(session, name="", surface="board", params={})


def test_name_whitespace_only_rejected(session):
    with pytest.raises(ValueError, match="name is required"):
        create_saved_view(session, name="   ", surface="board", params={})


def test_invalid_surface(session):
    with pytest.raises(ValueError, match="surface must be one of"):
        create_saved_view(session, name="X", surface="inbox", params={})


def test_board_surface_valid(session):
    v = create_saved_view(session, name="Board view", surface="board", params={"q": "test"})
    assert v.surface == "board"


def test_list_surface_valid(session):
    v = create_saved_view(session, name="List view", surface="list", params={"order": "priority"})
    assert v.surface == "list"


def test_board_unknown_param_rejected(session):
    with pytest.raises(ValueError, match="unknown params"):
        create_saved_view(session, name="X", surface="board", params={"bogus": "priority"})


def test_list_unknown_param_rejected(session):
    with pytest.raises(ValueError, match="unknown params"):
        create_saved_view(session, name="X", surface="list", params={"unknown": "value"})


def test_non_string_param_value_rejected(session):
    with pytest.raises(ValueError, match="must be a string"):
        create_saved_view(session, name="X", surface="board", params={"q": 42})


def test_surface_allowed_params_coverage():
    assert "board" in SURFACE_ALLOWED_PARAMS
    assert "list" in SURFACE_ALLOWED_PARAMS
    assert "q" in SURFACE_ALLOWED_PARAMS["board"]
    assert "group" in SURFACE_ALLOWED_PARAMS["board"]
    assert "order" in SURFACE_ALLOWED_PARAMS["board"]
    assert "props" in SURFACE_ALLOWED_PARAMS["board"]
    assert "q" in SURFACE_ALLOWED_PARAMS["list"]
    assert "group" in SURFACE_ALLOWED_PARAMS["list"]
    assert "order" in SURFACE_ALLOWED_PARAMS["list"]
    assert "props" in SURFACE_ALLOWED_PARAMS["list"]


def test_valid_surfaces_constant():
    assert "board" in VALID_SURFACES
    assert "list" in VALID_SURFACES
    assert "inbox" not in VALID_SURFACES


def test_empty_string_param_stripped(session):
    # Empty-string params carry no filter effect and should be stripped.
    v = create_saved_view(session, name="Clean", surface="board", params={"q": "", "group": "status"})
    assert "q" not in v.params
    assert v.params.get("group") == "status"


# ---------------------------------------------------------------------------
# Domain: URL composition
# ---------------------------------------------------------------------------


def test_view_url_board_no_params(session):
    v = create_saved_view(session, name="Board bare", surface="board", params={})
    assert view_url(v) == "/"


def test_view_url_board_with_q(session):
    v = create_saved_view(session, name="Board q", surface="board", params={"q": "status=review"})
    url = view_url(v)
    assert url.startswith("/?")
    assert "q=" in url
    assert "status" in url


def test_view_url_board_with_group(session):
    v = create_saved_view(session, name="Board group", surface="board", params={"group": "priority"})
    url = view_url(v)
    assert "group=priority" in url


def test_view_url_list_no_params(session):
    v = create_saved_view(session, name="List bare", surface="list", params={})
    assert view_url(v) == "/list"


def test_view_url_list_full_params(session):
    v = create_saved_view(
        session, name="List full", surface="list",
        params={"q": "project=nd", "group": "status", "order": "priority"},
    )
    url = view_url(v)
    assert url.startswith("/list?")
    assert "q=" in url
    assert "group=status" in url
    assert "order=priority" in url


def test_view_url_param_order_stable(session):
    # q must appear before group, group before order (deterministic).
    v = create_saved_view(
        session, name="Order test", surface="list",
        params={"order": "priority", "q": "test", "group": "status"},
    )
    url = view_url(v)
    q_idx = url.index("q=")
    group_idx = url.index("group=")
    order_idx = url.index("order=")
    assert q_idx < group_idx < order_idx


def test_view_url_special_chars_encoded(session):
    v = create_saved_view(
        session, name="Encoded", surface="board",
        params={"q": "project=my project"},
    )
    url = view_url(v)
    # Spaces must be percent-encoded (as + or %20).
    assert " " not in url


# ---------------------------------------------------------------------------
# Domain + URL: "tickets" surface (the live SPA surface)
# ---------------------------------------------------------------------------


def test_tickets_surface_valid():
    assert "tickets" in VALID_SURFACES
    assert "f" in SURFACE_ALLOWED_PARAMS["tickets"]
    assert "view" in SURFACE_ALLOWED_PARAMS["tickets"]


def test_tickets_surface_create_with_f_and_view(session):
    v = create_saved_view(
        session, name="Running board", surface="tickets",
        params={"f": "status:running", "view": "board"},
    )
    assert v.surface == "tickets"
    assert v.params == {"f": "status:running", "view": "board"}


def test_tickets_surface_accepts_legacy_keys(session):
    # Additive: legacy keys stay valid so nothing that used them breaks.
    v = create_saved_view(
        session, name="Legacy-ish", surface="tickets",
        params={"q": "project=nd", "group": "status"},
    )
    assert v.params == {"q": "project=nd", "group": "status"}


def test_tickets_surface_unknown_param_rejected(session):
    with pytest.raises(ValueError, match="unknown params"):
        create_saved_view(
            session, name="Bad", surface="tickets", params={"bogus": "x"},
        )


def test_view_url_tickets_with_f_and_view(session):
    v = create_saved_view(
        session, name="Running", surface="tickets",
        params={"f": "status:running", "view": "list"},
    )
    url = view_url(v)
    assert url.startswith("/tickets?")
    assert "view=list" in url
    # f is percent-encoded (the ":" becomes %3A).
    assert "f=" in url
    f_idx = url.index("f=")
    view_idx = url.index("view=")
    assert f_idx < view_idx


def test_view_url_tickets_empty_params(session):
    v = create_saved_view(session, name="Bare tickets", surface="tickets", params={})
    assert view_url(v) == "/tickets"


async def test_api_save_tickets_view_round_trips(bearer_client, session):
    r = await bearer_client.post(
        "/api/v1/views",
        json={
            "name": "SPA view",
            "surface": "tickets",
            "params": {"f": "label:bug", "view": "board"},
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["surface"] == "tickets"
    assert body["params"] == {"f": "label:bug", "view": "board"}
    assert body["url"].startswith("/tickets?")
    # Round-trips through the list endpoint too.
    lst = await bearer_client.get("/api/v1/views")
    assert lst.status_code == 200
    stored = next(x for x in lst.json() if x["id"] == body["id"])
    assert stored["params"] == {"f": "label:bug", "view": "board"}


# ---------------------------------------------------------------------------
# Routes: save (POST /views)
# ---------------------------------------------------------------------------


async def test_api_save_view_creates(bearer_client, session):
    r = await bearer_client.post(
        "/api/v1/views",
        json={"name": "Backend work", "surface": "board", "params": {"q": "project=backend"}},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Backend work"
    assert body["surface"] == "board"
    assert "url" in body
    views = list_saved_views(session)
    assert len(views) == 1


async def test_api_save_view_requires_auth(app, tmp_path):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/api/v1/views", json={"name": "X", "surface": "board", "params": {}})
    assert r.status_code == 401


async def test_api_save_view_duplicate_name_409(bearer_client, session):
    create_saved_view(session, name="Existing", surface="board", params={})
    r = await bearer_client.post(
        "/api/v1/views",
        json={"name": "Existing", "surface": "list", "params": {}},
    )
    assert r.status_code == 409


async def test_api_save_view_invalid_surface_422(bearer_client):
    r = await bearer_client.post(
        "/api/v1/views",
        json={"name": "Bad", "surface": "inbox", "params": {}},
    )
    assert r.status_code == 422


async def test_api_save_view_unknown_param_422(bearer_client):
    r = await bearer_client.post(
        "/api/v1/views",
        json={"name": "Bad", "surface": "board", "params": {"bogus": "priority"}},
    )
    assert r.status_code == 422


async def test_api_save_view_empty_name_422(bearer_client):
    r = await bearer_client.post(
        "/api/v1/views",
        json={"name": "", "surface": "board", "params": {}},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# JSON API: DELETE /api/v1/views/{id}
# ---------------------------------------------------------------------------


async def test_api_delete_view(bearer_client, session):
    v = create_saved_view(session, name="To remove", surface="board", params={})
    r = await bearer_client.delete(f"/api/v1/views/{v.id}")
    assert r.status_code == 204
    assert list_saved_views(session) == []


async def test_api_delete_view_not_found_404(bearer_client):
    r = await bearer_client.delete("/api/v1/views/nonexistent")
    assert r.status_code == 404


async def test_api_delete_view_requires_auth(app, tmp_path, session):
    v = create_saved_view(session, name="Protected", surface="board", params={})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.delete(f"/api/v1/views/{v.id}")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# JSON API: PATCH /api/v1/views/{id} (rename)
# ---------------------------------------------------------------------------


async def test_api_rename_view(bearer_client, session):
    v = create_saved_view(session, name="Old", surface="board", params={})
    r = await bearer_client.patch(f"/api/v1/views/{v.id}", json={"name": "New"})
    assert r.status_code == 200
    assert r.json()["name"] == "New"
    session.expire_all()
    assert get_saved_view(session, v.id).name == "New"


async def test_api_rename_view_conflict_409(bearer_client, session):
    create_saved_view(session, name="Alpha", surface="board", params={})
    v2 = create_saved_view(session, name="Beta", surface="list", params={})
    r = await bearer_client.patch(f"/api/v1/views/{v2.id}", json={"name": "Alpha"})
    assert r.status_code == 409


async def test_api_rename_view_not_found_404(bearer_client):
    r = await bearer_client.patch("/api/v1/views/bad-id", json={"name": "New"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# JSON API: POST /api/v1/views/reorder
# ---------------------------------------------------------------------------


async def test_api_reorder_views(bearer_client, session):
    a = create_saved_view(session, name="A", surface="board", params={})
    b = create_saved_view(session, name="B", surface="board", params={})
    r = await bearer_client.post(
        "/api/v1/views/reorder", json={"view_ids": [b.id, a.id]},
    )
    assert r.status_code == 200
    assert [v["id"] for v in r.json()] == [b.id, a.id]


# ---------------------------------------------------------------------------
# JSON API: GET /api/v1/views
# ---------------------------------------------------------------------------


async def test_api_list_views_empty(bearer_client):
    r = await bearer_client.get("/api/v1/views")
    assert r.status_code == 200
    assert r.json() == []


async def test_api_list_views_returns_composed_urls(bearer_client, session):
    create_saved_view(
        session, name="API view", surface="list",
        params={"q": "project=nd", "order": "priority"},
    )
    r = await bearer_client.get("/api/v1/views")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    item = data[0]
    assert item["name"] == "API view"
    assert item["surface"] == "list"
    assert "url" in item
    assert item["url"].startswith("/list")
    assert "q=" in item["url"]


async def test_api_list_views_includes_params(bearer_client, session):
    create_saved_view(session, name="P", surface="board", params={"group": "label"})
    r = await bearer_client.get("/api/v1/views")
    data = r.json()
    assert data[0]["params"]["group"] == "label"


async def test_api_list_views_requires_auth(app, tmp_path):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/v1/views")
    assert r.status_code == 401


async def test_api_views_accepts_cookie_auth(client):
    r = await client.get("/api/v1/views")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# End-to-end: save -> API list -> rename -> delete, all via /api/v1/views
# ---------------------------------------------------------------------------


async def test_e2e_api_round_trip(bearer_client, session):
    """Board view with q+group survives a full save -> API list -> URL round trip."""
    r = await bearer_client.post(
        "/api/v1/views",
        json={
            "name": "Round trip",
            "surface": "board",
            "params": {"q": "project=backend", "group": "priority"},
        },
    )
    assert r.status_code == 201
    view_id = r.json()["id"]

    r2 = await bearer_client.get("/api/v1/views")
    data = r2.json()
    assert len(data) == 1
    item = data[0]
    assert item["name"] == "Round trip"
    url = item["url"]
    assert url.startswith("/?") or url.startswith("/")
    assert "group=priority" in url
    assert "project" in url

    r3 = await bearer_client.patch(f"/api/v1/views/{view_id}", json={"name": "Renamed"})
    assert r3.status_code == 200

    r4 = await bearer_client.delete(f"/api/v1/views/{view_id}")
    assert r4.status_code == 204
    assert list_saved_views(session) == []


# ---------------------------------------------------------------------------
# Display options: props + order in allowed params
# ---------------------------------------------------------------------------


def test_board_allows_order_param(session):
    v = create_saved_view(
        session, name="board-order", surface="board",
        params={"q": "test", "order": "priority"},
    )
    assert v.params["order"] == "priority"


def test_board_allows_props_param(session):
    v = create_saved_view(
        session, name="board-props", surface="board",
        params={"props": "priority,labels"},
    )
    assert v.params["props"] == "priority,labels"


def test_list_allows_order_and_props(session):
    v = create_saved_view(
        session, name="list-full", surface="list",
        params={"order": "updated", "props": "labels,profile"},
    )
    assert v.params["order"] == "updated"
    assert v.params["props"] == "labels,profile"


def test_view_url_includes_props_and_order(session):
    """A saved list view with props + order composes the right URL."""
    v = create_saved_view(
        session, name="with-props", surface="list",
        params={"q": "alpha", "order": "priority", "props": "labels"},
    )
    url = view_url(v)
    assert url.startswith("/list?")
    assert "q=alpha" in url
    assert "order=priority" in url
    assert "props=labels" in url


def test_unknown_param_still_raises(session):
    with pytest.raises(ValueError, match="unknown params"):
        create_saved_view(
            session, name="bad", surface="board",
            params={"unknown_param": "foo"},
        )
