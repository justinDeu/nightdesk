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
        session, title="draft-ticket", prompt="x", profile_id=profile.id, status="draft", source_path="/tmp"
    )
    create_ticket(
        session, title="queued-ticket", prompt="x", profile_id=profile.id, status="queued", source_path="/tmp"
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
        session, title="edit-me", prompt="p", profile_id=profile.id, status="draft", source_path="/tmp"
    )
    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    assert "edit-me" in r.text
    assert "Delete" in r.text


async def test_sidebar_missing_ticket_404(cookie_client):
    r = await cookie_client.get("/board/sidebar?ticket_id=missing")
    assert r.status_code == 404


async def test_sidebar_create_cta_opens_create_modal(cookie_client, profile):
    """The empty-state CTA must be a real, keyboard-focusable <button> that
    opens the always-present create modal — not a static span pointing at the
    top bar."""
    r = await cookie_client.get("/board/sidebar")
    assert r.status_code == 200
    body = r.text
    assert "<button" in body
    assert "document.getElementById('ticket-create-modal').showModal()" in body


async def test_sidebar_edit_lists_runs(cookie_client, session, profile):
    """Selecting a ticket lists its runs in the sidebar, newest first, with the
    run-now lightning bolt and a worker-log download link."""
    from nightdesk.domain.runs import finish_run, start_run

    t = create_ticket(
        session, title="has-runs", prompt="p", profile_id=profile.id,
        status="review", source_path="/tmp",
    )
    older = start_run(
        session, ticket_id=t.id, worktree_path="/tmp/w",
        transcript_path="/tmp/t.log", pid=1, host="h",
    )
    finish_run(session, older.id, exit_status="success", error_summary=None)
    newer = start_run(
        session, ticket_id=t.id, worktree_path="/tmp/w",
        transcript_path="/tmp/t2.log", pid=2, host="h",
        started_as_run_now=True,
    )

    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    assert f'data-run-row="{newer.id}"' in body
    assert f'data-run-row="{older.id}"' in body
    # Newest run renders first (list_runs orders by started_at desc).
    assert body.index(f'data-run-row="{newer.id}"') < body.index(
        f'data-run-row="{older.id}"'
    )
    # Worker-log download link is present for each run.
    assert f'/runs/{newer.id}/log' in body
    # The run-now run carries the lightning bolt.
    assert "&#9889;" in body
    # Rows link to the ticket detail page rather than an in-sidebar swap.
    assert f'href="/tickets/{t.id}"' in body


async def test_sidebar_edit_no_runs_shows_empty_state(cookie_client, session, profile):
    t = create_ticket(
        session, title="no-runs", prompt="p", profile_id=profile.id,
        status="draft", source_path="/tmp",
    )
    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    assert "no runs yet" in r.text


async def test_board_grid_pins_row_height(cookie_client):
    """The board layout reserves the viewport height once, then lets the grid
    consume the remaining space under the filter bar so columns scroll
    internally and the sidebar stays pinned."""
    r = await cookie_client.get("/")
    assert r.status_code == 200
    assert 'style="height: calc(100vh - 6rem);"' in r.text
    assert 'id="board-grid"' in r.text
    assert "grid-rows-1" in r.text
    assert "flex-1 min-h-0 overflow-hidden" in r.text


async def test_create_ticket_via_form(cookie_client, session, profile):
    # The sidebar submits one form field per row (repeated `additional_dirs`).
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "from-form",
            "prompt": "p",
            "profile_id": profile.id,
            "source_path": "/tmp",
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


async def test_new_ticket_modal_uses_toolchain_picker(cookie_client, session):
    r = await cookie_client.get("/board/new-ticket-modal")
    assert r.status_code == 200
    body = r.text
    # Enable/disable are now checkboxes drawn from the known presets, not raw
    # free-text textareas (Issue B).
    assert 'name="toolchain_enable"' in body
    assert 'name="toolchain_disable"' in body
    assert "<textarea name=\"toolchain_enable\"" not in body
    assert "<textarea name=\"toolchain_disable\"" not in body
    assert "<textarea name=\"toolchain_extra_paths\"" not in body
    # The picker is populated from the available toolchains.
    assert 'value="user-python-tools"' in body
    assert 'value="rust-user-tools"' in body
    # Extra paths reuse the existing path-search component.
    assert 'name="toolchain_extra_paths"' in body
    assert "ndPathSuggest" in body
    assert "nd-suggest-host" in body


async def test_create_ticket_form_rejects_unknown_toolchain(cookie_client, session, profile):
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "bad-tc",
            "profile_id": profile.id,
            "source_path": "/tmp",
            "toolchain_form": "1",
            "toolchain_enable": ["does-not-exist"],
        },
    )
    assert r.status_code == 422
    assert "unknown toolchain" in r.text


async def test_create_ticket_form_accepts_known_toolchain_and_extra_paths(cookie_client, session, profile):
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "good-tc",
            "profile_id": profile.id,
            "source_path": "/tmp",
            "toolchain_form": "1",
            "toolchain_enable": ["user-python-tools"],
            # Repeated rows; duplicates are de-duped.
            "toolchain_extra_paths": ["./scripts/bin", "./scripts/bin"],
        },
    )
    assert r.status_code == 204
    created = next(t for t in list_tickets(session, status="draft")
                   if t.title == "good-tc")
    assert created.toolchain_overrides == {
        "enable": ["user-python-tools"],
        "disable": [],
        "extra_paths": ["./scripts/bin"],
    }


async def test_edit_modal_surfaces_unknown_toolchain_override(cookie_client, session, profile):
    t = create_ticket(
        session,
        title="stale-tc",
        prompt="",
        profile_id=profile.id,
        source_path="/tmp",
        toolchain_overrides={"enable": ["user-python-tools"]},
    )
    # Simulate a preset that was removed after the ticket selected it.
    t.toolchain_overrides = {"enable": ["since-deleted"], "disable": [], "extra_paths": []}
    session.commit()

    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    # The unknown name is clearly flagged, not silently dropped.
    assert "data-toolchain-stale" in body
    assert "since-deleted" in body


async def test_edit_modal_marks_inherited_toolchain(cookie_client, session, profile):
    """A toolchain inherited from the project shows an 'Inherited' badge."""
    project = create_project(
        session, name="InheritTest", source_path="/tmp/inherit",
        default_toolchains=["user-python-tools"],
    )
    t = create_ticket(
        session,
        title="inherited-tc",
        prompt="",
        profile_id=profile.id,
        project_id=project.id,
        source_path="/tmp",
        # Project defaults are applied to the ticket via apply_project_defaults,
        # so user-python-tools appears in the enable list.
    )
    # Verify the default was applied.
    assert t.toolchain_overrides["enable"] == ["user-python-tools"]

    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    # The inherited badge is rendered for the preset that came from the project.
    assert "Inherited" in body
    # The preset row is still a checkbox so the user can opt out.
    assert 'value="user-python-tools"' in body


async def test_edit_modal_marks_explicitly_enabled_toolchain(cookie_client, session, profile):
    """A toolchain explicitly enabled on a ticket (not from project) shows 'Enabled'."""
    t = create_ticket(
        session,
        title="explicit-tc",
        prompt="",
        profile_id=profile.id,
        source_path="/tmp",
        toolchain_overrides={"enable": ["rust-user-tools"], "disable": [], "extra_paths": []},
    )

    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    # Explicitly-enabled presets get an 'Enabled' badge.
    assert "Enabled" in body
    assert 'value="rust-user-tools"' in body


async def test_edit_modal_marks_disabled_toolchain(cookie_client, session, profile):
    """A disabled toolchain shows a 'Disabled' badge."""
    project = create_project(
        session, name="DisableTest", source_path="/tmp/disable",
        default_toolchains=["user-python-tools"],
    )
    t = create_ticket(
        session,
        title="disabled-tc",
        prompt="",
        profile_id=profile.id,
        project_id=project.id,
        source_path="/tmp",
        toolchain_overrides={
            "enable": [],
            "disable": ["user-python-tools"],
            "extra_paths": [],
        },
    )

    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    # The disabled badge appears for the opted-out preset.
    assert "Disabled" in body
    assert "user-python-tools" in body


async def test_sidebar_shows_toolchain_summary_with_provenance(cookie_client, session, profile):
    """The sidebar view-mode shows toolchain summary with inherited/enabled/disabled."""
    project = create_project(
        session, name="SidebarTC", source_path="/tmp/sidebar-tc",
        default_toolchains=["user-python-tools"],
    )
    t = create_ticket(
        session,
        title="sidebar-tc-ticket",
        prompt="",
        profile_id=profile.id,
        project_id=project.id,
        source_path="/tmp",
        toolchain_overrides={
            "enable": ["user-python-tools", "rust-user-tools"],
            "disable": [],
            "extra_paths": ["/opt/custom/bin"],
        },
    )

    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    # Sidebar shows the toolchain section header.
    assert "Toolchain" in body
    # Inherited from project.
    assert "Inherited" in body
    # Explicitly enabled (rust-user-tools is not a project default).
    assert "Enabled" in body
    # Extra path is shown.
    assert "/opt/custom/bin" in body


async def test_stale_toolchain_notes_project_default_origin(cookie_client, session, profile):
    """A stale toolchain that was a project default gets extra context."""
    project = create_project(
        session, name="StaleProject", source_path="/tmp/stale",
        default_toolchains=["user-python-tools"],
    )
    t = create_ticket(
        session,
        title="stale-origin",
        prompt="",
        profile_id=profile.id,
        project_id=project.id,
        source_path="/tmp",
        toolchain_overrides={"enable": ["user-python-tools"]},
    )
    # Simulate the preset being deleted after the ticket was created.
    t.toolchain_overrides = {"enable": ["since-deleted"], "disable": [], "extra_paths": []}
    # Update the project to no longer reference it either.
    project.default_toolchains = []
    session.commit()

    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    assert "data-toolchain-stale" in body
    assert "since-deleted" in body


async def test_create_ticket_via_form_accepts_multiple_linked_workspaces(cookie_client, session, profile):
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "multi-linked",
            "prompt": "p",
            "profile_id": profile.id,
            "source_path": "/tmp",
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
            "source_path": "/tmp",
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
        source_path="/tmp/old",
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
        data={"source_path": "/tmp/new"},
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
        params={"source_path": str(repo), "name": "feature"},
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
        params={"source_path": str(container), "name": "feature"},
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
        params={"source_path": str(container), "name": "asdf"},
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
        params={"source_path": str(container), "name": "asdf", "format": "json"},
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
        params={"source_path": f" {container} ", "name": "feature"},
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
            "source_path": "/tmp",
            "run_now": "1",
        },
    )
    assert r.status_code == 204
    [t] = list_tickets(session, status="draft")
    assert t.run_now is False


async def test_update_ticket_via_form(cookie_client, session, profile):
    t = create_ticket(
        session, title="old", prompt="op", profile_id=profile.id, status="draft", source_path="/tmp"
    )
    r = await cookie_client.post(
        f"/board/tickets/{t.id}",
        data={
            "title": "new",
            "prompt": "np",
            "profile_id": profile.id,
            "source_path": "/home/me/project",
        },
    )
    # Save returns the sidebar partial re-rendered in edit mode for the
    # same ticket so HTMX swaps the rail in place instead of reloading.
    assert r.status_code == 200
    assert "new" in r.text
    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.title == "new"
    assert after.workspaces[0].source_path == "/home/me/project"


async def test_delete_ticket(cookie_client, session, profile):
    t = create_ticket(
        session, title="kill-me", prompt="", profile_id=profile.id, status="draft", source_path="/tmp"
    )
    r = await cookie_client.request("DELETE", f"/board/tickets/{t.id}")
    assert r.status_code == 204
    assert list_tickets(session, status="draft") == []


async def test_move_endpoint_changes_status(cookie_client, session, profile):
    t = create_ticket(
        session, title="mover", prompt="", profile_id=profile.id, status="draft", source_path="/tmp"
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
        session, title="hurry", prompt="", profile_id=profile.id, status="queued", source_path="/tmp"
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
        session, title="a", prompt="", profile_id=profile.id, status="queued", source_path="/tmp"
    )
    b = create_ticket(
        session, title="b", prompt="", profile_id=profile.id, status="queued", source_path="/tmp"
    )
    c = create_ticket(
        session, title="c", prompt="", profile_id=profile.id, status="queued", source_path="/tmp"
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


async def test_create_rejects_missing_source_path(cookie_client, session, profile):
    """Form submit without a source path should 422, not silently land a NULL ticket."""
    r = await cookie_client.post(
        "/board/tickets",
        data={"title": "no-source-path", "profile_id": profile.id},
    )
    assert r.status_code == 422
    assert list_tickets(session, status="draft") == []


async def test_create_rejects_blank_source_path(cookie_client, session, profile):
    r = await cookie_client.post(
        "/board/tickets",
        data={"title": "blank-source-path", "profile_id": profile.id, "source_path": "   "},
    )
    assert r.status_code == 422
    assert list_tickets(session, status="draft") == []


async def test_create_expands_tilde_in_source_path(cookie_client, session, profile, monkeypatch):
    """A ~-prefixed source path from the autocomplete should round-trip as an absolute path."""
    monkeypatch.setenv("HOME", "/home/expanded")
    r = await cookie_client.post(
        "/board/tickets",
        data={"title": "tilde", "profile_id": profile.id, "source_path": "~/proj"},
    )
    assert r.status_code == 204
    [t] = list_tickets(session, status="draft")
    assert t.workspaces[0].source_path == "/home/expanded/proj"

async def test_board_project_filter_prefills_create_modal_workspaces(cookie_client, session, profile):
    create_project(
        session,
        name="Nightdesk",
        source_path="/tmp/nightdesk",
        default_workspace_mode="git_worktree",
        default_base_ref="main",
        default_linked_workspaces=[{
            "role": "linked",
            "label": "docs",
            "kind": "directory",
            "access": "read_only",
            "source_path": "/tmp/docs",
        }],
    )
    r = await cookie_client.get("/?project=nightdesk")
    assert r.status_code == 200
    assert 'value="/tmp/nightdesk"' in r.text
    assert 'value="/tmp/docs"' in r.text
    assert 'value="git_worktree"' in r.text

async def test_update_rejects_blank_cwd(cookie_client, session, profile):
    t = create_ticket(
        session, title="t", prompt="", profile_id=profile.id,
        status="draft", source_path="/home/me/project",
    )
    r = await cookie_client.post(
        f"/board/tickets/{t.id}",
        data={
            "title": "still-t",
            "profile_id": profile.id,
            "source_path": "",
        },
    )
    assert r.status_code == 422
    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.workspaces[0].source_path == "/home/me/project"  # unchanged


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
        status=ticket_status, source_path="/tmp",
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
        status="running", source_path="/tmp",
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
        status="queued", run_now=True, source_path="/tmp",
    )
    slow = create_ticket(
        session, title="slow",  prompt="", profile_id=profile.id,
        status="queued", run_now=False, source_path="/tmp",
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
        status="queued", source_path="/tmp",
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
        status="queued", run_now=True, source_path="/tmp",
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
        source_path="/tmp",
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


async def test_create_form_threads_base_ref(cookie_client, session, profile):
    """An explicit base_ref on the create form lands on the primary workspace."""
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "with-base-ref",
            "profile_id": profile.id,
            "source_path": "/tmp",
            "use_worktree": "1",
            "worktree_name": "feature",
            "base_ref": "develop",
        },
    )
    assert r.status_code == 204
    created = next(t for t in list_tickets(session, status="draft")
                   if t.title == "with-base-ref")
    primary = next(w for w in created.workspaces if w.role == "primary")
    assert primary.base_ref == "develop"


async def test_create_form_inherits_config_base_ref_default(cookie_client, session, profile):
    """A git-worktree ticket created without base_ref inherits the global default."""
    from nightdesk.db.models import ConfigRow
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t",
                          worktree_base_ref="develop"))
    session.commit()
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "inherits-default",
            "profile_id": profile.id,
            "source_path": "/tmp",
            "use_worktree": "1",
            "worktree_name": "feature",
        },
    )
    assert r.status_code == 204
    created = next(t for t in list_tickets(session, status="draft")
                   if t.title == "inherits-default")
    primary = next(w for w in created.workspaces if w.role == "primary")
    assert primary.base_ref == "develop"


async def test_create_form_explicit_base_ref_overrides_default(cookie_client, session, profile):
    """An explicit base_ref wins over the configured global default."""
    from nightdesk.db.models import ConfigRow
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t",
                          worktree_base_ref="develop"))
    session.commit()
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "explicit-wins",
            "profile_id": profile.id,
            "source_path": "/tmp",
            "use_worktree": "1",
            "worktree_name": "feature",
            "base_ref": "release-1.2",
        },
    )
    assert r.status_code == 204
    created = next(t for t in list_tickets(session, status="draft")
                   if t.title == "explicit-wins")
    primary = next(w for w in created.workspaces if w.role == "primary")
    assert primary.base_ref == "release-1.2"


async def test_directory_ticket_does_not_inherit_base_ref_default(cookie_client, session, profile):
    """A plain directory ticket never picks up the worktree base_ref default."""
    from nightdesk.db.models import ConfigRow
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t",
                          worktree_base_ref="develop"))
    session.commit()
    r = await cookie_client.post(
        "/board/tickets",
        data={
            "title": "plain-dir",
            "profile_id": profile.id,
            "source_path": "/tmp",
        },
    )
    assert r.status_code == 204
    created = next(t for t in list_tickets(session, status="draft")
                   if t.title == "plain-dir")
    primary = next(w for w in created.workspaces if w.role == "primary")
    assert primary.base_ref is None


async def test_worktree_preview_flags_missing_base_ref(cookie_client, tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)

    r = await cookie_client.get(
        "/board/worktree-preview",
        params={"source_path": str(repo), "name": "feat", "base_ref": "no-such-branch",
                "format": "json"},
    )
    assert r.status_code == 200
    assert r.json()["base_ref_status"] == "missing"

    r2 = await cookie_client.get(
        "/board/worktree-preview",
        params={"source_path": str(repo), "name": "feat", "base_ref": "HEAD",
                "format": "json"},
    )
    assert r2.status_code == 200
    assert r2.json()["base_ref_status"] == "ok"


async def test_worktree_preview_html_warns_about_missing_base_ref(cookie_client, tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)

    r = await cookie_client.get(
        "/board/worktree-preview",
        params={"source_path": str(repo), "name": "feat", "base_ref": "ghost"},
    )
    assert r.status_code == 200
    assert "not found" in r.text


async def test_sidebar_shows_branch_for_worktree_workspace(cookie_client, session, profile):
    """The sidebar Workspace section renders a Branch row when the primary
    workspace has a branch set (git_worktree tickets)."""
    t = create_ticket(
        session,
        title="branch-ticket",
        prompt="p",
        priority=0,
        profile_id=profile.id,
        source_path="/tmp",
        run_now=False,
        workspaces=[
            {
                "role": "primary",
                "label": "primary",
                "kind": "git_worktree",
                "access": "read_write",
                "source_path": "/tmp",
                "worktree_name": "feat-branch-name",
                "branch": "nightdesk/feat-branch-name",
                "retention": "preserve",
            },
        ],
    )
    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    assert "Branch:" in body
    assert "nightdesk/feat-branch-name" in body


async def test_sidebar_omits_branch_for_directory_workspace(cookie_client, session, profile):
    """A directory-mode ticket (no branch on the workspace) must not show a
    Branch row at all — no empty label or blank line."""
    t = create_ticket(
        session,
        title="dir-ticket",
        prompt="p",
        priority=0,
        profile_id=profile.id,
        source_path="/tmp",
        run_now=False,
    )
    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    assert "Branch:" not in body
