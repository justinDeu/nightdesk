from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, text

from nightdesk.db.models import Base, Ticket
from nightdesk.domain.tickets import clone_ticket, create_ticket, list_tickets, update_ticket


def test_project_table_is_registered_with_ticket_association():
    assert "projects" in Base.metadata.tables
    projects = Base.metadata.tables["projects"]
    assert "id" in projects.c
    assert "slug" in projects.c
    assert "source_path" in projects.c
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

    project = create_project(session, name="Night Desk", source_path="/tmp/nightdesk")
    archived = create_project(session, name="Old Repo", source_path="/tmp/old")
    archive_project(session, archived.id)

    assert project.slug == "night-desk"
    assert project.source_path == "/tmp/nightdesk"
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
        source_path="/tmp/nightdesk",
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
    assert ticket.workspaces[0].source_path == "/tmp/nightdesk"
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
        source_path="/tmp/nightdesk",
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


def test_project_rejects_unknown_toolchain_name(session):
    from nightdesk.domain.projects import create_project

    try:
        create_project(
            session,
            name="Bad tools",
            source_path="/tmp/bad-tools",
            default_toolchains=["pyhton-project"],
        )
    except ValueError as exc:
        assert "unknown toolchain" in str(exc)
    else:
        raise AssertionError("unknown toolchain was accepted")


def test_project_accepts_configured_toolchain_preset(session):
    from nightdesk.db.models import ConfigRow
    from nightdesk.domain.projects import create_project

    session.add(ConfigRow(
        id=1,
        worktree_root="/tmp/work",
        transcript_root="/tmp/transcripts",
        toolchain_presets={"custom-tools": ["/opt/custom/bin"]},
    ))
    session.commit()

    project = create_project(
        session,
        name="Configured tools",
        source_path="/tmp/configured-tools",
        default_toolchains=["custom-tools"],
    )

    assert project.default_toolchains == ["custom-tools"]



def test_ticket_rejects_unknown_toolchain_override(session, sample_profile):
    try:
        create_ticket(
            session,
            title="bad override",
            prompt="",
            profile_id=sample_profile.id,
            source_path="/tmp/bad-override",
            toolchain_overrides={"enable": ["missing-tools"]},
        )
    except ValueError as exc:
        assert "unknown toolchain" in str(exc)
    else:
        raise AssertionError("unknown toolchain override was accepted")


def test_toolchain_metadata_includes_labels_paths_and_descriptions():
    from nightdesk.domain.toolchains import toolchain_options

    options = {item["name"]: item for item in toolchain_options(None)}

    assert "python-project" not in options
    assert "node-project" not in options
    assert options["rust-user-tools"]["label"] == "Cargo-installed tools"
    assert options["rust-user-tools"]["paths"] == ["~/.cargo/bin"]
    assert "cargo install" in options["rust-user-tools"]["description"]
    assert options["user-python-tools"]["label"] == "User local tools"
    assert options["user-python-tools"]["paths"] == ["~/.local/bin"]
    assert "Any user-installed executables" in options["user-python-tools"]["description"]
def test_ticket_project_filters_and_update_clear_assignment(session, sample_profile):
    from nightdesk.domain.projects import create_project

    project = create_project(session, name="Nightdesk", source_path="/tmp/nightdesk")
    assigned = create_ticket(
        session,
        title="assigned",
        prompt="",
        profile_id=sample_profile.id,
        source_path="/tmp/nightdesk",
        project_id=project.id,
    )
    unassigned = create_ticket(
        session,
        title="unassigned",
        prompt="",
        profile_id=sample_profile.id,
        source_path="/tmp/other",
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

    project = create_project(session, name="Nightdesk", source_path="/tmp/nightdesk")
    ticket = create_ticket(
        session,
        title="parent",
        prompt="",
        profile_id=sample_profile.id,
        source_path="/tmp/nightdesk",
        project_id=project.id,
    )

    clone = clone_ticket(session, ticket.id, title="child", carry_context=False)

    assert clone.project_id == project.id


async def test_project_json_crud_and_ticket_project_filter(client):
    profile_id = await _create_profile(client)

    create_response = await client.post("/api/v1/projects", json={
        "name": "Nightdesk",
        "source_path": "/tmp/nightdesk",
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
    assert ticket["workspaces"][0]["source_path"] == "/tmp/nightdesk"
    assert ticket["workspaces"][0]["kind"] == "git_worktree"
    assert ticket["workspaces"][0]["worktree_name"] == "feat/add-project-api"

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
            "source_path": "/tmp",
            "project_id": "missing",
        })
        assert create_response.status_code == 404

        ticket_response = await client.post("/api/v1/tickets", json={
            "title": "good",
            "profile_id": profile_id,
            "source_path": "/tmp",
        })
        assert ticket_response.status_code == 201, ticket_response.text

        update_response = await client.patch(
            f"/api/v1/tickets/{ticket_response.json()['id']}",
            json={"project_id": "missing"},
        )
        assert update_response.status_code == 404















def test_toolchain_columns_are_registered():
    projects = Base.metadata.tables["projects"]
    tickets = Base.metadata.tables["tickets"]
    runs = Base.metadata.tables["runs"]
    config = Base.metadata.tables["config"]

    assert "default_toolchains" in projects.c
    assert "default_tool_paths" in projects.c
    assert "toolchain_overrides" in tickets.c
    assert "sandbox_tool_paths" in runs.c
    assert "toolchain_presets" in config.c


def test_project_stores_toolchain_defaults_and_ticket_stores_overrides(session, sample_profile):
    from nightdesk.domain.projects import create_project

    project = create_project(
        session,
        name="Tool Project",
        source_path="/tmp/tool-project",
        default_toolchains=["user-python-tools"],
        default_tool_paths=["./.venv/bin", "/opt/tooling/bin"],
    )

    ticket = create_ticket(
        session,
        title="Use extra tools",
        prompt="",
        profile_id=sample_profile.id,
        project_id=project.id,
        toolchain_overrides={
            "enable": ["rust-user-tools"],
            "disable": ["user-python-tools"],
            "extra_paths": ["./scripts/bin"],
        },
    )

    assert project.default_toolchains == ["user-python-tools"]
    assert project.default_tool_paths == ["./.venv/bin", "/opt/tooling/bin"]
    assert ticket.toolchain_overrides == {
        "enable": ["rust-user-tools"],
        "disable": ["user-python-tools"],
        "extra_paths": ["./scripts/bin"],
    }


def test_ticket_inherits_project_default_toolchains(session, sample_profile):
    """A new ticket that omits toolchains receives the project defaults as an
    ``enable`` override (Issue A)."""
    from nightdesk.domain.projects import create_project

    project = create_project(
        session,
        name="Defaults apply",
        source_path="/tmp/defaults",
        default_toolchains=["user-python-tools", "rust-user-tools"],
    )

    ticket = create_ticket(
        session,
        title="omits toolchains",
        prompt="",
        profile_id=sample_profile.id,
        project_id=project.id,
    )

    assert ticket.toolchain_overrides == {
        "enable": ["user-python-tools", "rust-user-tools"],
        "disable": [],
        "extra_paths": [],
    }


def test_ticket_explicit_toolchains_not_overwritten_by_project_defaults(session, sample_profile):
    from nightdesk.domain.projects import create_project

    project = create_project(
        session,
        name="Explicit wins",
        source_path="/tmp/explicit",
        default_toolchains=["user-python-tools"],
    )

    ticket = create_ticket(
        session,
        title="explicit override",
        prompt="",
        profile_id=sample_profile.id,
        project_id=project.id,
        toolchain_overrides={"enable": ["rust-user-tools"]},
    )

    assert ticket.toolchain_overrides["enable"] == ["rust-user-tools"]


def test_ticket_without_project_gets_no_default_toolchains(session, sample_profile):
    ticket = create_ticket(
        session,
        title="no project",
        prompt="",
        profile_id=sample_profile.id,
        source_path="/tmp/np",
    )
    assert ticket.toolchain_overrides is None


def test_ticket_without_project_defaults_keeps_overrides_none(session, sample_profile):
    from nightdesk.domain.projects import create_project

    project = create_project(session, name="No tool defaults", source_path="/tmp/none")

    ticket = create_ticket(
        session,
        title="empty defaults",
        prompt="",
        profile_id=sample_profile.id,
        project_id=project.id,
    )
    assert ticket.toolchain_overrides is None


def test_applied_project_default_toolchains_are_validated(session, sample_profile):
    """A default that is no longer a known preset must surface as an error when
    applied to a new ticket, never be silently accepted (Issue A keeps
    assert_known_toolchains intact)."""
    from nightdesk.domain.projects import create_project

    project = create_project(session, name="Stale defaults", source_path="/tmp/stale")
    # Simulate a preset that was deleted after the project chose it as a default,
    # bypassing the create-time validation that would otherwise reject it.
    project.default_toolchains = ["since-deleted"]
    session.commit()

    try:
        create_ticket(
            session,
            title="stale default",
            prompt="",
            profile_id=sample_profile.id,
            project_id=project.id,
        )
    except ValueError as exc:
        assert "unknown toolchain" in str(exc)
    else:
        raise AssertionError("unknown default toolchain was accepted")


def test_clone_ticket_preserves_toolchain_overrides(session, sample_profile):
    original = create_ticket(
        session,
        title="With tools",
        prompt="",
        profile_id=sample_profile.id,
        source_path="/tmp/with-tools",
        toolchain_overrides={
            "enable": ["rust-user-tools"],
            "extra_paths": ["./bin"],
        },
    )

    clone = clone_ticket(session, original.id, title=None, carry_context=False)

    assert clone.toolchain_overrides == {
        "enable": ["rust-user-tools"],
        "disable": [],
        "extra_paths": ["./bin"],
    }


def test_ticket_accepts_disable_of_unknown_toolchain(session, sample_profile):
    ticket = create_ticket(
        session,
        title="disable removed",
        prompt="",
        profile_id=sample_profile.id,
        source_path="/tmp/disable-removed",
        toolchain_overrides={"disable": ["preset-since-deleted"]},
    )

    assert ticket.toolchain_overrides == {
        "enable": [],
        "disable": ["preset-since-deleted"],
        "extra_paths": [],
    }


def test_project_rejects_default_tool_path_overlapping_protected_dir(session):
    import os
    from nightdesk.domain.projects import create_project

    bad = os.path.expanduser("~/.local/share/nightdesk/leak")
    try:
        create_project(
            session,
            name="Leaky",
            source_path="/tmp/leaky",
            default_tool_paths=[bad],
        )
    except ValueError as exc:
        assert "protected directory" in str(exc)
    else:
        raise AssertionError("protected-path tool dir was accepted")


def test_resolve_tool_paths_expands_env_vars(session, sample_profile, monkeypatch):
    import os
    from nightdesk.domain.projects import create_project
    from nightdesk.domain.toolchains import resolve_tool_paths

    monkeypatch.setenv("FAKE_TOOL_HOME", "/opt/fakehome")
    project = create_project(
        session,
        name="Env Tools",
        source_path="/tmp/env-tools",
        default_tool_paths=["$FAKE_TOOL_HOME/bin"],
    )
    ticket = create_ticket(
        session,
        title="env",
        prompt="",
        profile_id=sample_profile.id,
        project_id=project.id,
    )

    resolved = resolve_tool_paths(ticket=ticket, config=None, base_path="/tmp/env-tools")
    assert os.path.join("/opt/fakehome", "bin") in resolved


def test_ticket_rejects_extra_path_overlapping_protected_dir(session, sample_profile):
    import os

    bad = os.path.expanduser("~/.claude/peek")
    try:
        create_ticket(
            session,
            title="leak",
            prompt="",
            profile_id=sample_profile.id,
            source_path="/tmp/leak",
            toolchain_overrides={"extra_paths": [bad]},
        )
    except ValueError as exc:
        assert "protected directory" in str(exc)
    else:
        raise AssertionError("protected extra_path was accepted")


# ---------------------------------------------------------------------------
# Project defaults preview panel — domain + rendering tests
# ---------------------------------------------------------------------------


def test_preview_defaults_resolves_full_project_config(session):
    from nightdesk.domain.projects import create_project, preview_defaults

    project = create_project(
        session,
        name="Preview Full",
        source_path="/tmp/preview-full",
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
        default_toolchains=["user-python-tools"],
        default_tool_paths=["./.venv/bin"],
    )

    preview = preview_defaults(session, project)

    assert preview["source_path"] == "/tmp/preview-full"
    assert preview["workspace_mode"] == "git_worktree"
    assert preview["base_ref"] == "main"
    assert preview["worktree_name_template"] == "feat/{slug}"
    assert preview["worktree_name_resolved"] == "feat/example-ticket"
    assert len(preview["linked_workspaces"]) == 1
    assert preview["linked_workspaces"][0]["source_path"] == "/tmp/docs"
    assert preview["toolchains"] == ["user-python-tools"]
    assert preview["tool_paths"] == ["./.venv/bin"]
    assert preview["toolchain_overrides"] == {
        "enable": ["user-python-tools"],
        "disable": [],
        "extra_paths": [],
    }


def test_preview_defaults_minimal_project(session):
    from nightdesk.domain.projects import create_project, preview_defaults

    project = create_project(
        session,
        name="Preview Minimal",
        source_path="/tmp/preview-min",
    )

    preview = preview_defaults(session, project)

    assert preview["source_path"] == "/tmp/preview-min"
    assert preview["workspace_mode"] == "directory"
    assert preview["base_ref"] is None
    assert preview["worktree_name_template"] is None
    assert preview["worktree_name_resolved"] is None
    assert preview["linked_workspaces"] == []
    assert preview["toolchains"] == []
    assert preview["tool_paths"] == []
    assert preview["toolchain_overrides"] is None


def test_preview_defaults_no_toolchain_overrides_when_project_has_none(session):
    """A project with no default toolchains should produce None overrides."""
    from nightdesk.domain.projects import create_project, preview_defaults

    project = create_project(
        session,
        name="No Tool Defaults",
        source_path="/tmp/no-tools",
    )
    preview = preview_defaults(session, project)
    assert preview["toolchain_overrides"] is None








        # In directory mode, base_ref and worktree template should not appear
        # (they're conditional in the template)
