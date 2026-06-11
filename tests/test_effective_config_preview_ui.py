"""UI / rendering tests for the execution-context preview surfaces.

Covers the live create/edit preview (form-post path that parses workspaces,
toolchain overrides and additional dirs), progressive disclosure of long
lists, and the inclusion of the preview on the create/edit modal, board
sidebar, and ticket detail page across common project / profile / toolchain
combinations.
"""
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.tickets import create_ticket


def _profile(session, **overrides):
    kwargs = dict(
        name="P",
        fs_read=["/data"], fs_write=["/data"],
        allowed_tools=["Read"], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model="claude-sonnet", backend="claude_sdk",
    )
    kwargs.update(overrides)
    return create_profile(session, **kwargs)


def _fields(body):
    return {f["key"]: f for g in body["groups"] for f in g["fields"]}


def _field_html(body, key):
    marker = f'data-config-field="{key}"'
    start = body.index(marker)
    next_field = body.find('data-config-field="', start + len(marker))
    if next_field == -1:
        return body[start:]
    return body[start:next_field]


# --- live form-post preview: structured fields ------------------------------


async def test_form_preview_parses_toolchain_overrides(client, session):
    """A ticket-level toolchain enable from the form appears with ticket
    provenance — the live preview must reflect toolchain edits, not just the
    handful of scalar fields the old preview read."""
    p = _profile(session)
    r = await client.post(
        "/effective-config/preview",
        data={
            "profile_id": p.id,
            "source_path": "/tmp/proj",
            "toolchain_form": "1",
            "toolchain_enable": ["user-python-tools"],
        },
    )
    assert r.status_code == 200
    body = r.text
    assert "data-effective-config" in body
    assert "user-python-tools" in body
    # Enabled by the ticket overrides => ticket provenance chip present.
    assert 'data-provenance="ticket"' in body


async def test_form_preview_parses_workspace_worktree(client, session):
    """use_worktree + base_ref from the form drive the resolved workspace
    mode and base ref."""
    p = _profile(session)
    r = await client.post(
        "/effective-config/preview",
        data={
            "profile_id": p.id,
            "source_path": "/tmp/proj",
            "use_worktree": "1",
            "base_ref": "develop",
        },
    )
    assert r.status_code == 200
    j = await client.post(
        "/api/v1/effective-config/preview",
        json={
            "profile_id": p.id,
            "workspaces": [{
                "role": "primary", "kind": "git_worktree",
                "access": "read_write", "source_path": "/tmp/proj",
                "base_ref": "develop",
            }],
        },
    )
    fields = _fields(j.json())
    assert fields["workspace_mode"]["value"] == "git_worktree"
    assert fields["base_ref"]["value"] == "develop"


async def test_form_preview_parses_additional_dirs(client, session):
    """Additional dirs posted by the form become derived/ticket filesystem
    write reach in the preview."""
    p = _profile(session)
    r = await client.post(
        "/effective-config/preview",
        data={
            "profile_id": p.id,
            "source_path": "/tmp/proj",
            "additional_dirs": ["/srv/extra"],
        },
    )
    assert r.status_code == 200
    assert "/srv/extra" in r.text


async def test_form_preview_is_lenient_on_bad_path(client, session):
    """A half-typed / non-absolute source path must not 422 the live preview;
    it renders and the resolver flags the issue instead."""
    p = _profile(session)
    r = await client.post(
        "/effective-config/preview",
        data={"profile_id": p.id, "source_path": "relative/not/abs"},
    )
    assert r.status_code == 200
    assert "data-effective-config" in r.text


async def test_form_preview_no_profile_still_renders(client, session):
    r = await client.post("/effective-config/preview", data={"source_path": "/tmp"})
    assert r.status_code == 200
    assert "data-config-issue" in r.text
    assert "no profile" in r.text.lower()


# --- progressive disclosure -------------------------------------------------


async def test_long_list_uses_progressive_disclosure(client, session):
    """A profile with many filesystem-write entries collapses the overflow
    into a native <details> toggle while keeping every entry in the DOM."""
    paths = [f"/data/dir{i}" for i in range(8)]
    p = _profile(session, fs_write=paths, fs_read=paths)
    proj = create_project(session, name="Proj", source_path="/tmp/proj")
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
    )
    r = await client.get(f"/tickets/{t.id}/effective-config")
    assert r.status_code == 200
    body = r.text
    assert "data-config-overflow" in body
    assert "Show" in body and "more" in body
    # Every path is still present (collapsed, not dropped).
    for path in paths:
        assert path in body


async def test_short_list_has_no_disclosure(client, session):
    p = _profile(session, fs_write=["/data/a", "/data/b"])
    proj = create_project(session, name="Proj", source_path="/tmp/proj")
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
    )
    r = await client.get(f"/tickets/{t.id}/effective-config")
    assert r.status_code == 200
    assert "data-config-overflow" not in r.text


# --- provenance across project / profile / ticket layers --------------------


async def test_provenance_chips_span_layers(client, session):
    """A ticket that overrides a profile value on top of a project default
    surfaces all three provenance kinds at once."""
    p = _profile(session, default_model="claude-sonnet")
    proj = create_project(
        session, name="Proj", source_path="/tmp/proj",
        default_toolchains=["user-python-tools"],
    )
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
        permission_overrides={"default_model": "claude-opus"},
    )
    r = await client.get(f"/tickets/{t.id}/effective-config")
    body = r.text
    assert 'data-provenance="profile"' in body
    assert 'data-provenance="ticket"' in body
    assert 'data-provenance="project"' in body


async def test_empty_derived_rows_are_quiet(client, session):
    """Empty/default rows should say what the effective value is without
    presenting derived emptiness as meaningful configuration."""
    p = _profile(session, secret_keys=[])
    proj = create_project(session, name="Proj", source_path="/tmp/proj")
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
    )
    r = await client.get(f"/tickets/{t.id}/effective-config")
    assert r.status_code == 200
    body = r.text
    secret_keys = _field_html(body, "secret_keys")
    git_worktree = _field_html(body, "git_worktree")
    assert "data-config-empty" in secret_keys
    assert "Not set" in secret_keys
    assert 'data-provenance="derived"' not in secret_keys
    assert "data-config-empty" in git_worktree
    assert "Not used" in git_worktree
    assert 'data-provenance="derived"' not in git_worktree


# --- preview is mounted on every editing surface ----------------------------


async def test_create_modal_includes_live_preview(client, session):
    _profile(session)
    r = await client.get("/board/new-ticket-modal")
    assert r.status_code == 200
    body = r.text
    assert "data-effective-preview" in body
    assert 'hx-post="/effective-config/preview"' in body
    assert "data-effective-preview-section" in body


async def test_edit_modal_includes_live_preview(client, session):
    p = _profile(session)
    proj = create_project(session, name="Proj", source_path="/tmp/proj")
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
    )
    # The detail page renders the edit-mode modal inline (default modal id) and
    # also wires the read-only preview section. Both must be present, which
    # also exercises the edit-mode branches of the modal template.
    r = await client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    assert "data-effective-preview-section" in body  # live preview in edit modal
    assert 'hx-post="/effective-config/preview"' in body
    assert f'hx-get="/tickets/{t.id}/effective-config"' in body  # read-only section


async def test_sidebar_includes_readonly_preview(client, session):
    p = _profile(session)
    proj = create_project(session, name="Proj", source_path="/tmp/proj")
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
    )
    r = await client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    assert "data-sidebar-effective-config" in body
    assert "data-effective-config" in body
    assert f'hx-get="/tickets/{t.id}/effective-config"' not in body
    assert 'hx-target="this"' in body


async def test_sidebar_create_mode_has_no_preview(client, session):
    _profile(session)
    r = await client.get("/board/sidebar")
    assert r.status_code == 200
    # No ticket selected => nothing to resolve.
    assert "data-sidebar-effective-config" not in r.text
