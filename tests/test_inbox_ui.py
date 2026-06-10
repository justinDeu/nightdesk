"""Tests for the Inbox triage surface (under-specified work).

Covers:
  - Domain boundary: inbox tickets may be created without a workspace; the
    incomplete-ticket validation boundary blocks promotion until fleshed out;
    decline always works.
  - Routes/rendering: /inbox page + list fragment, capture, promote, decline,
    project-filtered views.
  - Actions: promote moves an item off the inbox onto the board; an incomplete
    item is rejected (422); decline archives.
  - Board isolation: inbox items never appear in the runnable board columns.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.tickets import (
    IncompleteTicket,
    InvalidTransition,
    create_ticket,
    decline_ticket,
    get_ticket,
    is_ticket_complete,
    list_inbox,
    promote_ticket,
    update_ticket,
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
        name="inbox-test",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


def _inbox(session, profile, *, title="Vague idea", complete=False, **kw):
    fields = dict(title=title, prompt="", status="inbox", profile_id=profile.id)
    if complete:
        fields["source_path"] = "/tmp"
    fields.update(kw)
    return create_ticket(session, **fields)


# ---------------------------------------------------------------------------
# Domain boundary
# ---------------------------------------------------------------------------


def test_inbox_ticket_can_be_created_without_workspace(session, profile):
    t = _inbox(session, profile)
    assert t.status == "inbox"
    assert t.workspaces == []
    assert not is_ticket_complete(t)


def test_non_inbox_still_requires_workspace(session, profile):
    with pytest.raises(ValueError):
        create_ticket(session, title="x", status="draft", profile_id=profile.id)


def test_promote_incomplete_is_blocked(session, profile):
    t = _inbox(session, profile)
    with pytest.raises(IncompleteTicket) as ei:
        promote_ticket(session, t.id, "draft")
    assert any("workspace" in r for r in ei.value.reasons)
    # IncompleteTicket is an InvalidTransition so existing handlers still catch it.
    assert isinstance(ei.value, InvalidTransition)
    assert get_ticket(session, t.id).status == "inbox"


def test_promote_after_fleshing_out_succeeds(session, profile):
    t = _inbox(session, profile)
    update_ticket(session, t.id, source_path="/tmp")
    promote_ticket(session, t.id, "queued")
    assert get_ticket(session, t.id).status == "queued"


def test_decline_archives_any_inbox_item(session, profile):
    t = _inbox(session, profile)  # incomplete
    decline_ticket(session, t.id)
    assert get_ticket(session, t.id).status == "archived"
    assert list_inbox(session) == []


def test_decline_rejects_non_inbox(session, profile):
    t = _inbox(session, profile, complete=True)
    promote_ticket(session, t.id, "draft")
    with pytest.raises(InvalidTransition):
        decline_ticket(session, t.id)


def test_list_inbox_project_filter(session, profile):
    p = create_project(session, name="Proj", source_path="/tmp")
    # Assigning a project injects a workspace, so this one is complete.
    with_proj = _inbox(session, profile, title="scoped", project_id=p.id)
    without = _inbox(session, profile, title="loose")
    assert {t.id for t in list_inbox(session)} == {with_proj.id, without.id}
    assert [t.id for t in list_inbox(session, project_id=p.id)] == [with_proj.id]
    assert [t.id for t in list_inbox(session, project_id="null")] == [without.id]


# ---------------------------------------------------------------------------
# Routes / rendering
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_inbox_page_renders(cookie_client, session, profile):
    _inbox(session, profile, title="Triage me")
    r = await cookie_client.get("/inbox")
    assert r.status_code == 200
    assert "Triage me" in r.text
    assert "Inbox" in r.text  # heading + nav
    # incomplete item shows the validation gate, not a promote button
    assert "Needs:" in r.text


@pytest.mark.anyio
async def test_inbox_nav_link_present(cookie_client, session, profile):
    r = await cookie_client.get("/inbox")
    assert 'href="/inbox"' in r.text


@pytest.mark.anyio
async def test_capture_creates_inbox_item(cookie_client, session, profile):
    r = await cookie_client.post(
        "/inbox/capture",
        data={"title": "Captured thing", "prompt": "later"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "Captured thing" in r.text
    items = list_inbox(session)
    assert len(items) == 1 and items[0].title == "Captured thing"
    assert items[0].status == "inbox"


@pytest.mark.anyio
async def test_capture_requires_title(cookie_client, session, profile):
    r = await cookie_client.post("/inbox/capture", data={"title": "  "})
    assert r.status_code == 422


@pytest.mark.anyio
async def test_capture_succeeds_with_zero_profiles(cookie_client, session):
    """Capture must not require any profiles to exist.

    The inbox is a capture queue; requiring a profile before an item can even
    be created contradicts the "jot it down, triage later" intent. The profile
    requirement is enforced at promotion time, not capture time.
    """
    # No profile fixture — the session has no profiles at all.
    r = await cookie_client.post(
        "/inbox/capture",
        data={"title": "No-profile capture", "prompt": "triage me later"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    items = list_inbox(session)
    assert len(items) == 1
    assert items[0].title == "No-profile capture"
    assert items[0].profile_id is None


@pytest.mark.anyio
async def test_promote_without_profile_is_422(cookie_client, session):
    """Promoting an inbox item that has no profile must return 422.

    capture -> profile_id=None -> try promote -> IncompleteTicket (profile missing).
    The item must stay in the inbox until a profile is set.
    """
    r = await cookie_client.post(
        "/inbox/capture",
        data={"title": "Needs a profile"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    items = list_inbox(session)
    assert len(items) == 1
    tid = items[0].id

    # Try to promote without giving it a workspace or profile.
    r = await cookie_client.post(
        f"/inbox/tickets/{tid}/promote",
        data={"target": "queued"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 422
    session.expire_all()
    assert get_ticket(session, tid).status == "inbox"
    assert "profile" in r.text.lower()


@pytest.mark.anyio
async def test_promote_route_blocks_incomplete(cookie_client, session, profile):
    t = _inbox(session, profile)
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data={"target": "queued"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 422
    assert get_ticket(session, t.id).status == "inbox"


@pytest.mark.anyio
async def test_promote_route_accepts_complete(cookie_client, session, profile):
    t = _inbox(session, profile, complete=True)
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data={"target": "draft"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()  # route committed via its own session
    assert get_ticket(session, t.id).status == "draft"
    # The refreshed list no longer contains the promoted item's title row.
    assert "0" in r.text  # count line: "0 items awaiting triage"


@pytest.mark.anyio
async def test_decline_route_archives(cookie_client, session, profile):
    t = _inbox(session, profile)
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/decline",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()  # route committed via its own session
    assert get_ticket(session, t.id).status == "archived"


@pytest.mark.anyio
async def test_promote_missing_ticket_404(cookie_client, session, profile):
    r = await cookie_client.post(
        "/inbox/tickets/nope/promote",
        data={"target": "draft"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 404


@pytest.mark.anyio
async def test_project_filtered_view(cookie_client, session, profile):
    p = create_project(session, name="Alpha", source_path="/tmp")
    _inbox(session, profile, title="scoped-item", project_id=p.id)
    _inbox(session, profile, title="loose-item")
    scoped = await cookie_client.get(f"/inbox?project={p.slug}")
    assert "scoped-item" in scoped.text and "loose-item" not in scoped.text
    loose = await cookie_client.get("/inbox?project=none")
    assert "loose-item" in loose.text and "scoped-item" not in loose.text


@pytest.mark.anyio
async def test_unknown_project_filter_shows_nothing(cookie_client, session, profile):
    _inbox(session, profile, title="loose-item")
    r = await cookie_client.get("/inbox?project=does-not-exist")
    assert r.status_code == 200
    assert "loose-item" not in r.text


# ---------------------------------------------------------------------------
# Board isolation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_inbox_excluded_from_board_columns(cookie_client, session, profile):
    _inbox(session, profile, title="INBOXONLYTITLE")
    r = await cookie_client.get("/board/columns")
    assert r.status_code == 200
    assert "INBOXONLYTITLE" not in r.text
