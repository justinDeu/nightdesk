from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, text

from nightdesk.db.models import Base, Ticket
from nightdesk.domain.tickets import clone_ticket, create_ticket, list_tickets, update_ticket


def test_project_table_is_registered_with_ticket_association():
    assert "projects" in Base.metadata.tables
    projects = Base.metadata.tables["projects"]
    assert "id" in projects.c
    assert "slug" in projects.c
    assert "cwd" in projects.c
    assert "default_workspace_mode" in projects.c
    assert "default_worktree_name_template" in projects.c
    assert "default_base_ref" in projects.c
    assert "default_linked_workspaces" in projects.c
    assert "archived_at" in projects.c

    assert hasattr(Ticket, "project_id")
    ticket_columns = {column.name for column in inspect(Ticket).columns}
    assert "project_id" in ticket_columns


async def _create_profile(client):
    response = await client.post("/api/v1/profiles", json={
        "name": "projects-test",
        "fs_read": [],
        "fs_write": [],
        "allowed_tools": [],
        "denied_tools": [],
        "network_mode": "off",
        "network_allowlist": [],
        "secret_keys": [],
        "default_model": None,
        "claude_credentials": {"source": "inherit"},
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _cookie_client(app):
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"nightdesk_token": "t"},
    )



def _setup_fts(session):
    session.execute(text(
        "CREATE VIRTUAL TABLE IF NOT EXISTS tickets_fts USING fts5("
        "title, prompt, id UNINDEXED)"
    ))
    session.commit()


def _index_ticket(session, ticket):
    session.execute(text(
        "INSERT INTO tickets_fts(rowid, title, prompt, id) "
        "VALUES ((SELECT rowid FROM tickets WHERE id=:id), :title, :prompt, :id)"
    ), {"id": ticket.id, "title": ticket.title, "prompt": ticket.prompt})
    session.commit()

def test_create_project_normalizes_slug_and_lists_active_projects(session):
    from nightdesk.domain.projects import archive_project, create_project, list_projects

    project = create_project(session, name="Night Desk", cwd="/tmp/nightdesk")
    archived = create_project(session, name="Old Repo", cwd="/tmp/old")
    archive_project(session, archived.id)

    assert project.slug == "night-desk"
    assert project.cwd == "/tmp/nightdesk"
    assert [p.id for p in list_projects(session)] == [project.id]
    assert {p.id for p in list_projects(session, include_archived=True)} == {
        project.id,
        archived.id,
    }


def test_ticket_creation_applies_project_workspace_defaults(session, sample_profile):
    from nightdesk.domain.projects import create_project

    project = create_project(
        session,
        name="Nightdesk",
        cwd="/tmp/nightdesk",
        default_workspace_mode="git_worktree",
        default_worktree_name_template="feat/{slug}",
        default_base_ref="main",
        default_linked_workspaces=[
            {
                "role": "linked",
                "label": "docs",
                "kind": "directory",
                "access": "read_only",
                "source_path": "/tmp/docs",
            }
        ],
    )

    ticket = create_ticket(
        session,
        title="Add Project Support",
        prompt="ship it",
        profile_id=sample_profile.id,
        project_id=project.id,
    )

    assert ticket.project_id == project.id
    assert ticket.cwd == "/tmp/nightdesk"
    assert ticket.workspace_mode == "git_worktree"
    primary = next(w for w in ticket.workspaces if w.role == "primary")
    linked = next(w for w in ticket.workspaces if w.role == "linked")
    assert primary.source_path == "/tmp/nightdesk"
    assert primary.kind == "git_worktree"
    assert primary.worktree_name == "feat/add-project-support"
    assert primary.base_ref == "main"
    assert linked.label == "docs"
    assert linked.source_path == "/tmp/docs"
    assert linked.access == "read_only"


def test_explicit_worktree_name_overrides_project_template(session, sample_profile):
    from nightdesk.domain.projects import create_project

    project = create_project(
        session,
        name="Nightdesk explicit",
        cwd="/tmp/nightdesk",
        default_workspace_mode="git_worktree",
        default_worktree_name_template="feat/{slug}",
    )

    ticket = create_ticket(
        session,
        title="Add Project Support",
        prompt="ship it",
        profile_id=sample_profile.id,
        project_id=project.id,
        worktree_name="custom-branch",
    )

    primary = next(w for w in ticket.workspaces if w.role == "primary")
    assert primary.worktree_name == "custom-branch"


def test_ticket_project_filters_and_update_clear_assignment(session, sample_profile):
    from nightdesk.domain.projects import create_project

    project = create_project(session, name="Nightdesk", cwd="/tmp/nightdesk")
    assigned = create_ticket(
        session,
        title="assigned",
        prompt="",
        profile_id=sample_profile.id,
        cwd="/tmp/nightdesk",
        project_id=project.id,
    )
    unassigned = create_ticket(
        session,
        title="unassigned",
        prompt="",
        profile_id=sample_profile.id,
        cwd="/tmp/other",
    )

    assert [t.id for t in list_tickets(session, project_id=project.id)] == [assigned.id]
    assert [t.id for t in list_tickets(session, project_id="null")] == [unassigned.id]

    update_ticket(session, assigned.id, project_id=None)

    assert {t.id for t in list_tickets(session, project_id="null")} == {
        assigned.id,
        unassigned.id,
    }


def test_clone_ticket_inherits_project_assignment(session, sample_profile):
    from nightdesk.domain.projects import create_project

    project = create_project(session, name="Nightdesk", cwd="/tmp/nightdesk")
    ticket = create_ticket(
        session,
        title="parent",
        prompt="",
        profile_id=sample_profile.id,
        cwd="/tmp/nightdesk",
        project_id=project.id,
    )

    clone = clone_ticket(session, ticket.id, title="child", carry_context=False)

    assert clone.project_id == project.id


async def test_project_json_crud_and_ticket_project_filter(client):
    profile_id = await _create_profile(client)

    create_response = await client.post("/api/v1/projects", json={
        "name": "Nightdesk",
        "cwd": "/tmp/nightdesk",
        "default_workspace_mode": "git_worktree",
        "default_worktree_name_template": "feat/{slug}",
        "default_base_ref": "main",
        "default_linked_workspaces": [
            {
                "role": "linked",
                "label": "docs",
                "kind": "directory",
                "access": "read_only",
                "source_path": "/tmp/docs",
            }
        ],
    })
    assert create_response.status_code == 201, create_response.text
    project = create_response.json()
    assert project["slug"] == "nightdesk"

    ticket_response = await client.post("/api/v1/tickets", json={
        "title": "Add Project API",
        "prompt": "",
        "profile_id": profile_id,
        "project_id": project["id"],
    })
    assert ticket_response.status_code == 201, ticket_response.text
    ticket = ticket_response.json()
    assert ticket["project_id"] == project["id"]
    assert ticket["cwd"] == "/tmp/nightdesk"
    assert ticket["workspace_mode"] == "git_worktree"
    assert ticket["worktree_name"] == "feat/add-project-api"

    filtered_response = await client.get(f"/api/v1/tickets?project_id={project['id']}")
    assert filtered_response.status_code == 200, filtered_response.text
    assert [item["id"] for item in filtered_response.json()] == [ticket["id"]]

    clear_response = await client.patch(f"/api/v1/tickets/{ticket['id']}", json={
        "project_id": None,
    })
    assert clear_response.status_code == 200, clear_response.text
    assert clear_response.json()["project_id"] is None

    null_response = await client.get("/api/v1/tickets?project_id=null")
    assert null_response.status_code == 200, null_response.text
    assert [item["id"] for item in null_response.json()] == [ticket["id"]]


async def test_ticket_api_rejects_unknown_project_id(app):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer t"},
    ) as client:
        profile_id = await _create_profile(client)

        create_response = await client.post("/api/v1/tickets", json={
            "title": "bad project",
            "profile_id": profile_id,
            "cwd": "/tmp",
            "project_id": "missing",
        })
        assert create_response.status_code == 404

        ticket_response = await client.post("/api/v1/tickets", json={
            "title": "good",
            "profile_id": profile_id,
            "cwd": "/tmp",
        })
        assert ticket_response.status_code == 201, ticket_response.text

        update_response = await client.patch(
            f"/api/v1/tickets/{ticket_response.json()['id']}",
            json={"project_id": "missing"},
        )
        assert update_response.status_code == 404


async def test_settings_projects_ui_creates_project(app, session):
    async with await _cookie_client(app) as client:
        page = await client.get("/settings/projects")
        assert page.status_code == 200
        assert 'href="/settings/projects"' in page.text
        assert 'name="name"' in page.text
        assert 'name="cwd"' in page.text
        assert 'name="slug"' not in page.text
        assert 'data-slug-preview' in page.text
        assert 'name="color"' not in page.text
        assert "focus:bg-bg-elev-2" in page.text
        assert 'name="default_workspace_mode"' in page.text
        assert 'data-worktree-template-fields' in page.text
        assert 'data-linked-workspaces' in page.text
        assert "{slug}" in page.text
        assert "project-create-modal-cwd-suggest" in page.text
        assert "ndPathSuggest" in page.text
        assert 'data-create-project-toggle' in page.text
        assert "Create project" in page.text
        assert "showModal()" in page.text

        created = await client.post("/settings/projects", data={
            "name": "Night Desk",
            "cwd": "/tmp/nightdesk",
            "default_workspace_mode": "git_worktree",
            "default_worktree_name_template": "feat/{slug}",
            "default_base_ref": "main",
            "linked_workspace_path": "/tmp/docs",
            "linked_workspace_kind": "directory",
            "linked_workspace_access": "read_only",
        })
        assert created.status_code == 200, created.text
        assert "Night Desk" in created.text
        assert "/tmp/nightdesk" in created.text

    from nightdesk.domain.projects import list_projects
    projects = list_projects(session)
    assert [(p.name, p.slug, p.cwd, p.default_linked_workspaces) for p in projects] == [
        ("Night Desk", "night-desk", "/tmp/nightdesk", [{
            "role": "linked",
            "label": "docs",
            "kind": "directory",
            "access": "read_only",
            "source_path": "/tmp/docs",
        }])
    ]


async def test_settings_projects_ui_edits_existing_project(app, session):
    from nightdesk.domain.projects import create_project, list_projects

    project = create_project(
        session,
        name="Night Desk",
        cwd="/tmp/nightdesk",
        default_workspace_mode="directory",
    )

    async with await _cookie_client(app) as client:
        page = await client.get("/settings/projects")
        assert page.status_code == 200
        assert f"project-edit-modal-{project.id}" in page.text
        assert "Edit" in page.text

        updated = await client.post(f"/settings/projects/{project.id}", data={
            "name": "Night Desk Updated",
            "cwd": "/tmp/nightdesk-updated",
            "default_workspace_mode": "git_worktree",
            "default_worktree_name_template": "feat/{slug}",
            "default_base_ref": "main",
            "linked_workspace_path": "/tmp/docs",
            "linked_workspace_kind": "directory",
            "linked_workspace_access": "read_only",
        })
        assert updated.status_code == 200, updated.text
        assert "Night Desk Updated" in updated.text
        assert "/tmp/nightdesk-updated" in updated.text

    session.expire_all()
    refreshed = list_projects(session)
    assert [(p.name, p.cwd, p.default_workspace_mode, p.default_worktree_name_template, p.default_base_ref, p.default_linked_workspaces) for p in refreshed] == [
        (
            "Night Desk Updated",
            "/tmp/nightdesk-updated",
            "git_worktree",
            "feat/{slug}",
            "main",
            [{
                "role": "linked",
                "label": "docs",
                "kind": "directory",
                "access": "read_only",
                "source_path": "/tmp/docs",
            }],
        )
    ]


async def test_board_project_filter_and_ticket_form_assignment(app, session, sample_profile):
    from nightdesk.domain.projects import create_project

    project = create_project(session, name="Nightdesk", cwd="/tmp/nightdesk")
    create_ticket(
        session,
        title="project ticket",
        prompt="",
        profile_id=sample_profile.id,
        cwd="/tmp/nightdesk",
        project_id=project.id,
    )
    create_ticket(
        session,
        title="other ticket",
        prompt="",
        profile_id=sample_profile.id,
        cwd="/tmp/other",
    )

    async with await _cookie_client(app) as client:
        board = await client.get("/?project=nightdesk")
        assert board.status_code == 200
        assert "project ticket" in board.text
        assert "other ticket" not in board.text
        assert '<option value="nightdesk" selected' in board.text
        assert f'<option value="{project.id}" selected' in board.text

        created = await client.post("/board/tickets", data={
            "title": "from project form",
            "prompt": "",
            "profile_id": sample_profile.id,
            "project_id": project.id,
            "cwd": "/tmp/nightdesk",
        })
        assert created.status_code == 204

    session.expire_all()
    ticket = next(t for t in list_tickets(session) if t.title == "from project form")
    assert ticket.project_id == project.id


async def test_archive_and_header_search_project_filters(app, session, sample_profile):
    from nightdesk.domain.projects import create_project
    from nightdesk.domain.tickets import transition_status, archive

    project = create_project(session, name="Nightdesk", cwd="/tmp/nightdesk")
    kept = create_ticket(
        session,
        title="dark mode project",
        prompt="",
        profile_id=sample_profile.id,
        cwd="/tmp/nightdesk",
        status="queued",
        project_id=project.id,
    )
    hidden = create_ticket(
        session,
        title="dark mode other",
        prompt="",
        profile_id=sample_profile.id,
        cwd="/tmp/other",
        status="queued",
    )
    for ticket in (kept, hidden):
        transition_status(session, ticket.id, "running")
        transition_status(session, ticket.id, "review")
        archive(session, ticket.id)
    _setup_fts(session)
    _index_ticket(session, kept)
    _index_ticket(session, hidden)

    async with await _cookie_client(app) as client:
        archive_page = await client.get("/archive?project=nightdesk&since=&until=")
        assert archive_page.status_code == 200
        assert "dark mode project" in archive_page.text
        assert "dark mode other" not in archive_page.text
        assert '<option value="nightdesk" selected' in archive_page.text

        search = await client.get("/header/search?q=dark&project=nightdesk")
        assert search.status_code == 200
        assert "dark mode project" in search.text
        assert "dark mode other" not in search.text
        assert "Nightdesk" in search.text


async def test_settings_projects_empty_state_uses_simple_help_text(app):
    async with await _cookie_client(app) as client:
        page = await client.get("/settings/projects")
        assert page.status_code == 200
        assert 'data-projects-empty-state' in page.text
        assert "No projects yet." in page.text
        assert "Use Create project to add one." in page.text
        assert "Create your first project" not in page.text
