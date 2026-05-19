"""Tests for the v1 profile editor (HTML + JSON surfaces) and seeding."""
from __future__ import annotations

import io
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nightdesk.api.app import create_app
from nightdesk.db.models import Base, Profile
from nightdesk.domain.profiles import (
    create_profile,
    list_profiles,
    seed_default_profiles,
)


# ---------------------------------------------------------------------------
# Fixtures: scoped local engines so seeding tests don't bleed into others.
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_engine():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def fresh_app(fresh_engine, tmp_path):
    return create_app(
        engine=fresh_engine,
        bearer_token="t",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "tx",
        worktree_root=tmp_path / "w",
    )


@pytest.fixture
async def fresh_client(fresh_app):
    transport = ASGITransport(app=fresh_app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer t"},
    ) as c:
        yield c


@pytest.fixture
async def cookie_client(fresh_app):
    transport = ASGITransport(app=fresh_app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Seeding.
# ---------------------------------------------------------------------------


def test_seed_default_profiles_creates_three_named_presets(fresh_engine):
    created = seed_default_profiles(fresh_engine)
    assert len(created) == 3
    with Session(fresh_engine) as s:
        names = {p.name for p in list_profiles(s)}
    assert names == {"Read only", "Edit workspace", "Full workspace"}


def test_seed_default_profiles_is_idempotent(fresh_engine):
    seed_default_profiles(fresh_engine)
    second = seed_default_profiles(fresh_engine)
    assert second == []
    with Session(fresh_engine) as s:
        assert len(list_profiles(s)) == 3


def test_seeded_profile_attributes_match_spec(fresh_engine):
    seed_default_profiles(fresh_engine)
    with Session(fresh_engine) as s:
        by_name = {p.name: p for p in list_profiles(s)}
    ro = by_name["Read only"]
    assert ro.network_mode == "off"
    assert ro.permission_mode == "default"
    assert ro.allowed_tools == ["Read", "Grep", "Glob"]
    assert ro.denied_tools == ["Bash", "Edit", "Write"]
    assert "Inspect" in ro.description
    edit = by_name["Edit workspace"]
    assert edit.network_mode == "on"
    assert edit.permission_mode == "acceptEdits"
    full = by_name["Full workspace"]
    assert full.permission_mode == "bypassPermissions"


# ---------------------------------------------------------------------------
# JSON API: credential encryption + redaction.
# ---------------------------------------------------------------------------


async def test_json_create_persists_encrypted_credentials(fresh_client, fresh_engine):
    """Posting an api_key credential encrypts the secret at rest and redacts
    it in the response. The literal plaintext must never appear in the row."""
    payload = {
        "name": "with-creds",
        "claude_credentials": {"source": "api_key", "value": "sk-test-XYZ"},
    }
    r = await fresh_client.post("/api/v1/profiles", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    # Response is redacted — no plaintext leaks back.
    assert body["claude_credentials"] == {"source": "api_key", "value_set": True, "base_url": None}
    assert "sk-test-XYZ" not in r.text

    # Row at rest is ciphertext, not the literal value.
    with Session(fresh_engine) as s:
        p = s.get(Profile, body["id"])
        assert p.claude_credentials is not None
        assert "sk-test-XYZ" not in p.claude_credentials


async def test_json_get_redacts_credentials(fresh_client):
    create_r = await fresh_client.post("/api/v1/profiles", json={
        "name": "redact-me",
        "claude_credentials": {"source": "auth_token", "value": "tk-abc"},
    })
    pid = create_r.json()["id"]
    r = await fresh_client.get(f"/api/v1/profiles/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["claude_credentials"] == {"source": "auth_token", "value_set": True, "base_url": None}
    assert "tk-abc" not in r.text


async def test_json_env_round_trips_as_keys_only(fresh_client, fresh_engine):
    """env values are encrypted; the response exposes only the key list."""
    r = await fresh_client.post("/api/v1/profiles", json={
        "name": "env-profile",
        "env": {"FOO": "secret-foo", "BAR": "secret-bar"},
        "claude_credentials": {"source": "inherit"},
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert sorted(body["env_keys"]) == ["BAR", "FOO"]
    assert "secret-foo" not in r.text
    with Session(fresh_engine) as s:
        p = s.get(Profile, body["id"])
        assert p.env is not None
        assert "secret-foo" not in p.env


# ---------------------------------------------------------------------------
# Forbidden field stripping on import.
# ---------------------------------------------------------------------------


async def test_import_strips_forbidden_fields(cookie_client):
    payload = {
        "name": "imported",
        "description": "from elsewhere",
        "network_mode": "off",
        "hooks": [{"type": "PreToolUse"}],
        "mcpServers": {"foo": {"command": "x"}},
        "agents": [{"name": "a"}],
        "skills": ["s"],
    }
    files = {"file": ("p.json", io.BytesIO(json.dumps(payload).encode()),
                       "application/json")}
    r = await cookie_client.post(
        "/profiles/import",
        files=files,
        headers={"accept": "application/json"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body["dropped_fields"]) >= {"hooks", "mcpServers", "agents", "skills"}


# ---------------------------------------------------------------------------
# Path validation overlapping protected dirs.
# ---------------------------------------------------------------------------


async def test_fs_write_rejects_protected_paths(fresh_client):
    import os
    home = os.path.expanduser("~")
    bad_path = os.path.join(home, ".config", "nightdesk")
    r = await fresh_client.post("/api/v1/profiles", json={
        "name": "bad-paths",
        "fs_write": [bad_path],
        "claude_credentials": {"source": "inherit"},
    })
    assert r.status_code == 400, r.text
    assert "fs_write" in r.json().get("detail", "")


# ---------------------------------------------------------------------------
# HTML editor smoke checks.
# ---------------------------------------------------------------------------


async def test_html_new_form_renders_credential_radios(cookie_client):
    r = await cookie_client.get("/profiles/new")
    assert r.status_code == 200
    body = r.text
    assert 'name="credentials_source"' in body
    for src in ("inherit", "api_key", "auth_token"):
        assert f'value="{src}"' in body


async def test_html_view_shows_profile_name_and_edit_link(cookie_client, fresh_engine):
    """The /profiles/{id} page renders the profile in view mode by default.
    The right pane carries an explicit Edit link to /profiles/{id}/edit so
    the user can flip into the form without accidentally mutating state on
    a passive page visit."""
    with Session(fresh_engine) as s:
        seed_default_profiles(fresh_engine)
        profiles = list_profiles(s)
        target = next(p for p in profiles if p.name == "Read only")
        pid = target.id

    r = await cookie_client.get(f"/profiles/{pid}")
    assert r.status_code == 200
    body = r.text
    assert "Read only" in body
    assert f'href="/profiles/{pid}/edit"' in body


async def test_html_edit_url_shows_form_posting_to_save(cookie_client, fresh_engine):
    """Explicit /edit url renders the form, which POSTs to /profiles/{id}."""
    with Session(fresh_engine) as s:
        seed_default_profiles(fresh_engine)
        target = next(p for p in list_profiles(s) if p.name == "Read only")
        pid = target.id

    r = await cookie_client.get(f"/profiles/{pid}/edit")
    assert r.status_code == 200
    body = r.text
    assert f'action="/profiles/{pid}"' in body
    # The backend dropdown ships as part of every editor render.
    assert 'name="backend"' in body


async def test_html_list_page_shows_seeded_profiles(cookie_client, fresh_engine):
    seed_default_profiles(fresh_engine)
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    body = r.text
    assert "Read only" in body
    assert "Edit workspace" in body
    assert "Full workspace" in body
    # New-profile entrypoint must be visible.
    assert 'href="/profiles/new"' in body


async def test_html_list_page_renders_two_pane_with_default_selection(
    cookie_client, fresh_engine,
):
    """With existing profiles, the list page picks the first profile and
    renders it in view mode so the right pane isn't empty on first load."""
    seed_default_profiles(fresh_engine)
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    body = r.text
    # Sidebar shell.
    assert 'id="profile-pane"' in body
    # Right pane is in view mode for the first profile (alphabetical:
    # "Edit workspace") and exposes an Edit link rather than the form.
    assert 'href="/profiles/' in body and '/edit"' in body


async def test_html_edit_form_includes_disabled_backend_choices(
    cookie_client, fresh_engine,
):
    """v1 ships one backend; the dropdown should list claude_sdk and nothing
    else (no 'coming soon' vaporware entries)."""
    seed_default_profiles(fresh_engine)
    with Session(fresh_engine) as s:
        pid = next(p.id for p in list_profiles(s) if p.name == "Read only")
    r = await cookie_client.get(f"/profiles/{pid}/edit")
    body = r.text
    assert 'value="claude_sdk"' in body
    assert "coming soon" not in body.lower()
    assert 'value="codex_cli"' not in body
    assert 'value="openai_responses"' not in body


async def test_html_edit_form_lists_known_env_vars_in_picker(
    cookie_client, fresh_engine,
):
    """The env-var picker exposes the catalog so users don't have to guess
    at exact names. Each option carries its description in a data attribute
    that the inline JS surfaces when the user adds the row."""
    seed_default_profiles(fresh_engine)
    with Session(fresh_engine) as s:
        pid = next(p.id for p in list_profiles(s) if p.name == "Read only")
    r = await cookie_client.get(f"/profiles/{pid}/edit")
    body = r.text
    assert 'id="env-picker"' in body
    # A handful of well-known names should appear in the picker.
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "DISABLE_TELEMETRY"):
        assert name in body
    # Descriptions are wired through as data-description attrs.
    assert "data-description=" in body


async def test_form_create_rejects_unknown_backend(cookie_client):
    """Posting an unknown backend from the form is rejected so curl callers
    can't bypass the dropdown and persist a backend Nightdesk can't run."""
    r = await cookie_client.post(
        "/profiles",
        data={
            "name": "bad-backend",
            "backend": "made_up_backend",
            "permission_mode": "default",
            "network_mode": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400, r.text
    assert "backend" in r.json().get("detail", "").lower()


async def test_htmx_pane_returns_fragment_not_full_page(
    cookie_client, fresh_engine,
):
    """An HX-Request GET on /profiles/{id} returns only the right-pane
    fragment so clicking sidebar links swaps the pane in place without
    re-rendering the sidebar (which would lose the user's scroll position
    and any pending form state)."""
    seed_default_profiles(fresh_engine)
    with Session(fresh_engine) as s:
        pid = next(p.id for p in list_profiles(s) if p.name == "Read only")

    r = await cookie_client.get(
        f"/profiles/{pid}",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.text
    # Fragment starts at the pane div; no surrounding <html> chrome.
    assert "<html" not in body.lower()
    assert 'id="profile-pane"' in body
    assert "Read only" in body


async def test_catalog_lookup_returns_new_entries():
    """The expanded catalog should expose every variable the editor promotes."""
    from nightdesk.domain.cc_env_catalog import categories, lookup

    for name in (
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "MAX_THINKING_TOKENS",
        "DISABLE_BUG_COMMAND",
        "DISABLE_COST_WARNINGS",
        "DISABLE_ERROR_REPORTING",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "BASH_DEFAULT_TIMEOUT_MS",
        "MCP_TIMEOUT",
        "ANTHROPIC_BETAS",
    ):
        entry = lookup(name)
        assert entry is not None, f"missing catalog entry: {name}"
        assert entry.description
    # Category bucketing still works and covers the new sections.
    cat_names = {c for c, _ in categories()}
    for required in ("Models", "Behavior", "Bash tool", "MCP"):
        assert required in cat_names, f"missing category: {required}"


async def test_html_edit_form_renders_promoted_sections(cookie_client, fresh_engine):
    """The editor must render Authentication, Models, and Behavior sections."""
    from nightdesk.domain.profiles import list_profiles, seed_default_profiles

    seed_default_profiles(fresh_engine)
    with Session(fresh_engine) as s:
        pid = next(p.id for p in list_profiles(s) if p.name == "Read only")
    r = await cookie_client.get(f"/profiles/{pid}/edit")
    body = r.text
    # Auth section owns ANTHROPIC_BASE_URL and the secret value; no separate
    # routing/proxy fieldset.
    assert "Authentication" in body
    assert 'name="credentials_source"' in body
    assert 'name="credentials_base_url"' in body
    assert "Routing / proxy" not in body
    assert "<legend" in body
    # Model rows render the catalog name + a free-text input.
    for key in (
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        assert f'name="promoted_model_{key}"' in body, key
    assert 'name="promoted_model_ANTHROPIC_SMALL_FAST_MODEL"' not in body
    # Behavior toggles.
    for key in (
        "DISABLE_TELEMETRY",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_BUG_COMMAND",
        "DISABLE_COST_WARNINGS",
        "DISABLE_ERROR_REPORTING",
        "DISABLE_PROMPT_CACHING",
    ):
        assert f'name="promoted_behavior_{key}"' in body, key
    assert 'name="promoted_max_thinking_tokens"' in body


async def test_promoted_model_persists_into_env_blob(
    cookie_client, fresh_client, fresh_engine,
):
    """Setting ANTHROPIC_MODEL via the dedicated section writes the env blob."""
    from nightdesk.domain.profile_secrets import ProfileSecretBox
    from nightdesk.db.models import Profile

    create_r = await fresh_client.post("/api/v1/profiles", json={
        "name": "model-promote",
        "claude_credentials": {"source": "inherit"},
    })
    pid = create_r.json()["id"]

    r = await cookie_client.post(
        f"/profiles/{pid}",
        data={
            "name": "model-promote",
            "backend": "claude_sdk",
            "permission_mode": "default",
            "network_mode": "on",
            "promoted_env_submitted": "1",
            "promoted_model_ANTHROPIC_MODEL": "claude-opus-4-5",
            "promoted_routing_base_url": "",
            "promoted_routing_auth_token": "",
            "promoted_max_thinking_tokens": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    box = ProfileSecretBox("t")
    with Session(fresh_engine) as s:
        p = s.get(Profile, pid)
        decoded = box.decrypt(p.env)
    assert decoded.get("ANTHROPIC_MODEL") == "claude-opus-4-5"


async def test_promoted_behavior_toggle_round_trip(
    cookie_client, fresh_client, fresh_engine,
):
    """Toggling DISABLE_TELEMETRY on then off adds and removes the key."""
    from nightdesk.domain.profile_secrets import ProfileSecretBox
    from nightdesk.db.models import Profile

    create_r = await fresh_client.post("/api/v1/profiles", json={
        "name": "toggle-test",
        "claude_credentials": {"source": "inherit"},
    })
    pid = create_r.json()["id"]
    box = ProfileSecretBox("t")

    # Tick the checkbox.
    r = await cookie_client.post(
        f"/profiles/{pid}",
        data={
            "name": "toggle-test",
            "backend": "claude_sdk",
            "permission_mode": "default",
            "network_mode": "on",
            "promoted_env_submitted": "1",
            "promoted_behavior_DISABLE_TELEMETRY": "1",
            "promoted_routing_base_url": "",
            "promoted_routing_auth_token": "",
            "promoted_max_thinking_tokens": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with Session(fresh_engine) as s:
        p = s.get(Profile, pid)
        decoded = box.decrypt(p.env)
    assert decoded.get("DISABLE_TELEMETRY") == "1"

    # Untick (omit the field entirely, as unchecked checkboxes don't post).
    r = await cookie_client.post(
        f"/profiles/{pid}",
        data={
            "name": "toggle-test",
            "backend": "claude_sdk",
            "permission_mode": "default",
            "network_mode": "on",
            "promoted_env_submitted": "1",
            "promoted_routing_base_url": "",
            "promoted_routing_auth_token": "",
            "promoted_max_thinking_tokens": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with Session(fresh_engine) as s:
        p = s.get(Profile, pid)
        decoded = box.decrypt(p.env) if p.env else {}
    assert "DISABLE_TELEMETRY" not in (decoded or {})


async def test_generic_env_picker_excludes_promoted_keys(
    cookie_client, fresh_engine,
):
    """Promoted keys must not appear as <option> values in the generic picker."""
    from nightdesk.domain.profiles import list_profiles, seed_default_profiles

    seed_default_profiles(fresh_engine)
    with Session(fresh_engine) as s:
        pid = next(p.id for p in list_profiles(s) if p.name == "Read only")
    r = await cookie_client.get(f"/profiles/{pid}/edit")
    body = r.text
    # The picker still exists and lists a non-promoted variable.
    assert 'id="env-picker"' in body
    assert '<option value="HTTPS_PROXY"' in body
    # Promoted keys must not appear as picker options. They DO appear
    # elsewhere on the page (in the dedicated fieldsets) so a substring
    # check on the bare name would be a false positive; check for the
    # exact <option value="..."> wrapper.
    for promoted in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "DISABLE_TELEMETRY",
        "MAX_THINKING_TOKENS",
    ):
        assert f'<option value="{promoted}"' not in body, promoted


async def test_view_pane_redacts_credentials_and_env_values(
    cookie_client, fresh_client, fresh_engine,
):
    """View mode never leaks the encrypted value: credentials show only the
    source label and env vars render as keys + 'value set'."""
    create_r = await fresh_client.post("/api/v1/profiles", json={
        "name": "view-redact",
        "claude_credentials": {"source": "api_key", "value": "sk-leak-me"},
        "env": {"ANTHROPIC_CUSTOM_HEADERS": "X-Leak: https://leak.example/"},
    })
    pid = create_r.json()["id"]

    r = await cookie_client.get(f"/profiles/{pid}")
    body = r.text
    assert "sk-leak-me" not in body
    assert "https://leak.example/" not in body
    # Sanity: the var name itself does appear (so users can audit what's set).
    assert "ANTHROPIC_CUSTOM_HEADERS" in body
    assert "value set" in body
