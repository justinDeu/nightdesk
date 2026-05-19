import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import (
    create_ticket,
    get_ticket,
    list_tickets,
)


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
        name="board-test",
        fs_read=[],
        fs_write=[],
        allowed_tools=[],
        denied_tools=[],
        network_mode="off",
        network_allowlist=[],
        secret_keys=[],
        default_model=None,
    )


async def test_board_root_renders_columns(cookie_client):
    r = await cookie_client.get("/")
    assert r.status_code == 200
    body = r.text
    for label in ("Draft", "Queued", "Running", "Review"):
        assert label in body, f"missing column label {label}"


async def test_board_shows_tickets_in_each_column(cookie_client, session, profile):
    create_ticket(
        session, title="draft-ticket", prompt="x", profile_id=profile.id, status="draft", cwd="/tmp"
    )
    create_ticket(
        session, title="queued-ticket", prompt="x", profile_id=profile.id, status="queued", cwd="/tmp"
    )
    r = await cookie_client.get("/")
    assert r.status_code == 200
    assert "draft-ticket" in r.text
    assert "queued-ticket" in r.text


async def test_sidebar_create_mode_default(cookie_client, profile):
    r = await cookie_client.get("/board/sidebar")
    assert r.status_code == 200
    assert "New ticket" in r.text


async def test_sidebar_edit_mode(cookie_client, session, profile):
    t = create_ticket(
        session, title="edit-me", prompt="p", profile_id=profile.id, status="draft", cwd="/tmp"
    )
    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    assert "edit-me" in r.text
    assert "Delete" in r.text


async def test_sidebar_missing_ticket_404(cookie_client):
    r = await cookie_client.get("/board/sidebar?ticket_id=missing")
    assert r.status_code == 404


async def test_create_ticket_via_form(cookie_client, session, profile):
    # The sidebar submits one form field per row (repeated `additional_dirs`).
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "from-form",
            "prompt": "p",
            "profile_id": profile.id,
            "cwd": "/tmp",
            "additional_dirs": ["/home/me/a", "/home/me/b"],
        },
    )
    assert r.status_code == 204
    assert r.headers.get("HX-Redirect") == "/"
    tickets = list_tickets(session, status="draft")
    titles = [t.title for t in tickets]
    assert "from-form" in titles
    created = next(t for t in tickets if t.title == "from-form")
    assert created.additional_dirs == [
        {"path": "/home/me/a", "mode": "rw"},
        {"path": "/home/me/b", "mode": "rw"},
    ]


async def test_create_ticket_via_form_accepts_multiple_linked_workspaces(cookie_client, session, profile):
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "multi-linked",
            "prompt": "p",
            "profile_id": profile.id,
            "cwd": "/tmp",
            "use_worktree": "1",
            "worktree_name": "feature",
            "linked_workspace_path": ["/home/me/api", "/home/me/docs"],
            "linked_workspace_kind": ["git_worktree", "directory"],
            "linked_workspace_access": ["read_write", "read_only"],
        },
    )

    assert r.status_code == 204
    created = next(t for t in list_tickets(session, status="draft")
                   if t.title == "multi-linked")
    assert [w.source_path for w in created.workspaces] == [
        "/tmp", "/home/me/api", "/home/me/docs",
    ]
    assert [w.kind for w in created.workspaces] == [
        "git_worktree", "git_worktree", "directory",
    ]
    assert [w.access for w in created.workspaces] == [
        "read_write", "read_write", "read_only",
    ]
    assert [w.worktree_name for w in created.workspaces] == [
        "feature", "feature", None,
    ]


async def test_board_form_forces_linked_git_worktree_read_write(cookie_client, session, profile):
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "linked-git-forced-rw",
            "profile_id": profile.id,
            "cwd": "/tmp",
            "use_worktree": "1",
            "worktree_name": "feature",
            "linked_workspace_path": "/home/me/api",
            "linked_workspace_kind": "git_worktree",
            "linked_workspace_access": "read_only",
        },
    )

    assert r.status_code == 204
    created = next(t for t in list_tickets(session, status="draft")
                   if t.title == "linked-git-forced-rw")
    assert created.workspaces[1].access == "read_write"
    assert created.workspaces[1].worktree_name == "feature"

async def test_partial_board_update_preserves_linked_workspaces(cookie_client, session, profile):
    t = create_ticket(
        session,
        title="partial-workspace",
        prompt="p",
        profile_id=profile.id,
        cwd="/tmp/old",
        workspaces=[
            {
                "role": "primary",
                "label": "primary",
                "kind": "directory",
                "access": "read_write",
                "source_path": "/tmp/old",
            },
            {
                "role": "linked",
                "label": "docs",
                "kind": "directory",
                "access": "read_only",
                "source_path": "/srv/docs",
            },
        ],
    )

    r = await cookie_client.post(
        f"/board/tickets/{t.id}",
        data={"cwd": "/tmp/new"},
    )

    assert r.status_code == 200
    session.expire_all()
    refreshed = session.get(type(t), t.id)
    assert [w.role for w in refreshed.workspaces] == ["primary", "linked"]
    assert refreshed.workspaces[0].source_path == "/tmp/new"
    assert refreshed.workspaces[1].source_path == "/srv/docs"


async def test_worktree_preview_resolves_default_path(cookie_client, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    r = await cookie_client.get(
        "/board/worktree-preview",
        params={"cwd": str(repo), "name": "feature"},
    )

    assert r.status_code == 200
    assert str(tmp_path / "work" / "repo" / "feature") in r.text


async def test_worktree_preview_uses_bare_container_root(cookie_client, tmp_path):
    import subprocess

    container = tmp_path / "stack"
    bare = container / ".bare"
    container.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    (container / ".git").write_text("gitdir: ./.bare\n")

    r = await cookie_client.get(
        "/board/worktree-preview",
        params={"cwd": str(container), "name": "feature"},
    )

    assert r.status_code == 200
    assert str(container / "feature") in r.text
    assert "bare-container layout" in r.text


async def test_worktree_preview_formats_source_as_title_suffix(cookie_client, tmp_path):
    import subprocess

    container = tmp_path / "stack"
    bare = container / ".bare"
    container.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    (container / ".git").write_text("gitdir: ./.bare\n")

    r = await cookie_client.get(
        "/board/worktree-preview",
        params={"cwd": str(container), "name": "asdf"},
    )

    assert r.status_code == 200
    assert "Worktree Path Preview (bare-container layout)" in r.text
    assert "Resolved path" not in r.text
    assert str(container / "asdf") in r.text


async def test_worktree_preview_returns_json_for_stable_dom_updates(cookie_client, tmp_path):
    import subprocess

    container = tmp_path / "stack"
    bare = container / ".bare"
    container.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    (container / ".git").write_text("gitdir: ./.bare\n")

    r = await cookie_client.get(
        "/board/worktree-preview",
        params={"cwd": str(container), "name": "asdf", "format": "json"},
    )

    assert r.status_code == 200
    assert r.json() == {
        "path": str(container / "asdf"),
        "source": "bare-container layout",
    }


async def test_worktree_preview_strips_cwd_whitespace(cookie_client, tmp_path):
    import subprocess

    container = tmp_path / "stack"
    bare = container / ".bare"
    container.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    (container / ".git").write_text("gitdir: ./.bare\n")

    r = await cookie_client.get(
        "/board/worktree-preview",
        params={"cwd": f" {container} ", "name": "feature"},
    )

    assert r.status_code == 200
    assert str(container / "feature") in r.text
async def test_create_form_ignores_run_now_param(cookie_client, session, profile):
    """The create form no longer exposes a run_now checkbox. Posting one (e.g.
    from a stale tab) is silently ignored — run_now is an action, not a
    create-time attribute."""
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "runny",
            "profile_id": profile.id,
            "cwd": "/tmp",
            "run_now": "1",
        },
    )
    assert r.status_code == 204
    [t] = list_tickets(session, status="draft")
    assert t.run_now is False


async def test_update_ticket_via_form(cookie_client, session, profile):
    t = create_ticket(
        session, title="old", prompt="op", profile_id=profile.id, status="draft", cwd="/tmp"
    )
    r = await cookie_client.post(
        f"/board/tickets/{t.id}",
        data={
            "title": "new",
            "prompt": "np",
            "profile_id": profile.id,
            "cwd": "/home/me/project",
        },
    )
    # Save returns the sidebar partial re-rendered in edit mode for the
    # same ticket so HTMX swaps the rail in place instead of reloading.
    assert r.status_code == 200
    assert "new" in r.text
    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.title == "new"
    assert after.cwd == "/home/me/project"


async def test_delete_ticket(cookie_client, session, profile):
    t = create_ticket(
        session, title="kill-me", prompt="", profile_id=profile.id, status="draft", cwd="/tmp"
    )
    r = await cookie_client.request("DELETE", f"/board/tickets/{t.id}")
    assert r.status_code == 204
    assert list_tickets(session, status="draft") == []


async def test_move_endpoint_changes_status(cookie_client, session, profile):
    t = create_ticket(
        session, title="mover", prompt="", profile_id=profile.id, status="draft", cwd="/tmp"
    )
    r = await cookie_client.post(
        f"/board/tickets/{t.id}/move",
        data={"target_status": "queued", "position": "0"},
    )
    assert r.status_code == 204
    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "queued"


async def test_move_to_running_sets_run_now(cookie_client, session, profile):
    t = create_ticket(
        session, title="hurry", prompt="", profile_id=profile.id, status="queued", cwd="/tmp"
    )
    r = await cookie_client.post(
        f"/board/tickets/{t.id}/move",
        data={"target_status": "running", "position": "0"},
    )
    assert r.status_code == 204
    session.expire_all()
    after = get_ticket(session, t.id)
    # v2: drag-to-running flags run_now and parks in queued so the worker
    # picks it up on the next tick. The API never transitions to running
    # directly — only the worker does, after creating a Run row.
    assert after.status == "queued"
    assert after.run_now is True


async def test_reorder_within_column(cookie_client, session, profile):
    a = create_ticket(
        session, title="a", prompt="", profile_id=profile.id, status="queued", cwd="/tmp"
    )
    b = create_ticket(
        session, title="b", prompt="", profile_id=profile.id, status="queued", cwd="/tmp"
    )
    c = create_ticket(
        session, title="c", prompt="", profile_id=profile.id, status="queued", cwd="/tmp"
    )
    # Reverse order.
    r = await cookie_client.post(
        "/board/columns/queued/reorder",
        data={"ticket_ids": f"{c.id},{b.id},{a.id}"},
    )
    assert r.status_code == 204
    session.expire_all()
    ordered = list_tickets(session, status="queued")
    assert [t.id for t in ordered] == [c.id, b.id, a.id]


async def test_unauthenticated_board_blocked(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/")
        assert r.status_code == 401


async def test_create_rejects_missing_cwd(cookie_client, session, profile):
    """Form submit without a cwd should 422, not silently land a NULL ticket."""
    r = await cookie_client.post(
        "/board/tickets",
        data={"title": "no-cwd", "profile_id": profile.id},
    )
    assert r.status_code == 422
    assert list_tickets(session, status="draft") == []


async def test_create_rejects_blank_cwd(cookie_client, session, profile):
    r = await cookie_client.post(
        "/board/tickets",
        data={"title": "blank-cwd", "profile_id": profile.id, "cwd": "   "},
    )
    assert r.status_code == 422
    assert list_tickets(session, status="draft") == []


async def test_create_expands_tilde_in_cwd(cookie_client, session, profile, monkeypatch):
    """A ~-prefixed cwd from the autocomplete should round-trip as an absolute path."""
    monkeypatch.setenv("HOME", "/home/expanded")
    r = await cookie_client.post(
        "/board/tickets",
        data={"title": "tilde", "profile_id": profile.id, "cwd": "~/proj"},
    )
    assert r.status_code == 204
    [t] = list_tickets(session, status="draft")
    assert t.cwd == "/home/expanded/proj"


async def test_update_rejects_blank_cwd(cookie_client, session, profile):
    t = create_ticket(
        session, title="t", prompt="", profile_id=profile.id,
        status="draft", cwd="/home/me/project",
    )
    r = await cookie_client.post(
        f"/board/tickets/{t.id}",
        data={
            "title": "still-t",
            "profile_id": profile.id,
            "cwd": "",
        },
    )
    assert r.status_code == 422
    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.cwd == "/home/me/project"  # unchanged


async def test_sidebar_create_has_no_run_now_checkbox(cookie_client, profile):
    """The sidebar create form must not expose a run_now checkbox.
    Run-now is an action, not a create-time attribute."""
    r = await cookie_client.get("/board/sidebar")
    assert r.status_code == 200
    body = r.text
    assert 'name="run_now"' not in body
    # And no Run now button either — there's no ticket yet to act on.
    assert "Run now" not in body


@pytest.mark.parametrize("ticket_status", ["draft", "queued", "review", "archived"])
async def test_sidebar_edit_shows_run_now_action(cookie_client, session, profile,
                                                   ticket_status):
    """The sidebar review pane for an existing ticket exposes the Run now
    action (HTMX-posts to /tickets/{id}/run-now) and never the old checkbox.

    The button is HTMX-wired rather than a plain form so clicking it doesn't
    navigate away from the board (hard requirement on the run-now ticket).
    """
    t = create_ticket(
        session, title="rn", prompt="", profile_id=profile.id,
        status=ticket_status, cwd="/tmp",
    )
    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    assert 'name="run_now"' not in body
    # Must use hx-post, not a plain <form action="...">, so the browser
    # stays on the board after the click.
    assert f'hx-post="/tickets/{t.id}/run-now"' in body
    assert f'action="/tickets/{t.id}/run-now"' not in body
    assert "Run now" in body


async def test_sidebar_edit_running_hides_run_now_action(cookie_client, session, profile):
    """The Run now action is hidden for tickets already running — nothing to
    bypass."""
    t = create_ticket(
        session, title="rn", prompt="", profile_id=profile.id,
        status="running", cwd="/tmp",
    )
    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    assert 'name="run_now"' not in body
    assert f'hx-post="/tickets/{t.id}/run-now"' not in body
    assert f'action="/tickets/{t.id}/run-now"' not in body


# --- columns polling + visual bucketing --------------------------------------


def _section_html(body: str, status: str) -> str:
    """Slice out the <section id="board-col-<status>"> block from a response.

    Lets the tests assert about which tickets render in which visual
    column without hand-rolling an HTML parser. Returns "" if the section
    is missing so a misuse fails the containment assertion loudly.
    """
    marker = f'id="board-col-{status}"'
    start = body.find(marker)
    if start == -1:
        return ""
    # Walk back to the opening <section ...> tag, then forward to its </section>.
    open_tag = body.rfind("<section", 0, start)
    close = body.find("</section>", start)
    if open_tag == -1 or close == -1:
        return ""
    return body[open_tag:close + len("</section>")]


async def test_run_now_queued_renders_in_running_column(
    cookie_client, session, profile,
):
    """A queued ticket with run_now=True visually belongs to running.

    This is the snap-back fix: after a drag-to-running parks the ticket
    in queued (with run_now=true), the next render must place it in the
    running column or the card visually bounces back to queued on the
    next poll.
    """
    fast = create_ticket(
        session, title="fast",  prompt="", profile_id=profile.id,
        status="queued", run_now=True, cwd="/tmp",
    )
    slow = create_ticket(
        session, title="slow",  prompt="", profile_id=profile.id,
        status="queued", run_now=False, cwd="/tmp",
    )
    r = await cookie_client.get("/")
    assert r.status_code == 200
    queued_section = _section_html(r.text, "queued")
    running_section = _section_html(r.text, "running")
    assert f'data-ticket-id="{fast.id}"' in running_section
    assert f'data-ticket-id="{fast.id}"' not in queued_section
    assert f'data-ticket-id="{slow.id}"' in queued_section
    assert f'data-ticket-id="{slow.id}"' not in running_section


async def test_columns_endpoint_returns_oob_sections(
    cookie_client, session, profile,
):
    """/board/columns is the polled fragment: four OOB column sections.

    The response carries no top-level body wrapper because the polling
    element uses hx-swap="none" and relies entirely on
    ``hx-swap-oob="true"`` on each section to update the board in place.
    """
    create_ticket(
        session, title="poll-me", prompt="", profile_id=profile.id,
        status="queued", cwd="/tmp",
    )
    r = await cookie_client.get("/board/columns")
    assert r.status_code == 200
    body = r.text
    for status in ("draft", "queued", "running", "review"):
        assert f'id="board-col-{status}"' in body, f"missing column {status}"
    # Each column section opts into out-of-band swap; without this htmx
    # would have no way to splice them into the existing DOM.
    assert body.count('hx-swap-oob="true"') == 4
    # The polled ticket shows up where expected.
    assert "poll-me" in _section_html(body, "queued")


async def test_drag_to_queued_clears_run_now(cookie_client, session, profile):
    """Dragging a run-now-queued card back to the queued column cancels
    the run-now intent. Otherwise the render bucketing would keep the
    card in the running column and the drag would visually bounce back.
    """
    t = create_ticket(
        session, title="cooldown", prompt="", profile_id=profile.id,
        status="queued", run_now=True, cwd="/tmp",
    )
    r = await cookie_client.post(
        f"/board/tickets/{t.id}/move",
        data={"target_status": "queued", "position": "0"},
    )
    assert r.status_code == 204
    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "queued"
    assert after.run_now is False


async def test_drag_to_queued_from_review_works(cookie_client, session, profile):
    """Requeueing from review via drag should still succeed; the
    run_now-clearing path must not break legitimate cross-column drops.
    """
    t = create_ticket(
        session, title="redo", prompt="", profile_id=profile.id, status="review",
        cwd="/tmp",
    )
    r = await cookie_client.post(
        f"/board/tickets/{t.id}/move",
        data={"target_status": "queued", "position": "0"},
    )
    assert r.status_code == 204
    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "queued"


async def test_columns_endpoint_blocked_without_auth(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/board/columns")
        assert r.status_code == 401
