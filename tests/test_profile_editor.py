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


async def test_binary_field_separated_from_auth_with_resolved_placeholder(cookie_client):
    """The Claude Code binary field lives in its own fieldset (not Authentication)
    and its placeholder shows the resolved daemon-default binary, not a generic
    hint."""
    r = await cookie_client.get("/profiles/new")
    assert r.status_code == 200
    body = r.text
    # Its own section heading, separate from auth.
    assert "Claude Code binary" in body
    # The input carries a concrete resolved placeholder (config override, else
    # PATH `claude`, else the literal "claude") — never the old generic hint.
    assert 'name="claude_binary_path"' in body
    assert "(daemon default)" not in body  # old placeholder text is gone
    # The binary input must NOT sit inside the Authentication fieldset: the
    # Authentication legend appears before "Claude Code binary", and the input
    # appears after the binary legend.
    assert body.index("Authentication") < body.index("Claude Code binary")
    assert body.index("Claude Code binary") < body.index('name="claude_binary_path"')


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


async def test_html_edit_form_lists_capability_backend_choices(
    cookie_client, fresh_engine,
):
    """The dropdown is driven by the capability descriptors: the two wired
    backends (claude_sdk, omp_rpc) appear and no vaporware entries do."""
    seed_default_profiles(fresh_engine)
    with Session(fresh_engine) as s:
        pid = next(p.id for p in list_profiles(s) if p.name == "Read only")
    r = await cookie_client.get(f"/profiles/{pid}/edit")
    body = r.text
    assert 'value="claude_sdk"' in body
    assert 'value="omp_rpc"' in body
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


async def test_edit_save_blank_env_value_keeps_existing(
    cookie_client, fresh_client, fresh_engine,
):
    """Regression: saving the edit form with a blank env value keeps it.

    The editor renders existing env keys with empty value inputs (secrets are
    never echoed) plus an ``env_rotate`` marker, and the helper text promises
    "leave blank to keep". A plain save must not silently wipe the value.
    """
    from nightdesk.domain.profile_secrets import ProfileSecretBox

    create_r = await fresh_client.post("/api/v1/profiles", json={
        "name": "keep-env",
        "claude_credentials": {"source": "inherit"},
        "env": {"FOO": "secret-foo"},
    })
    pid = create_r.json()["id"]

    r = await cookie_client.post(
        f"/profiles/{pid}",
        data={
            "name": "keep-env",
            "env_field_submitted": "1",
            "env_name": "FOO",
            "env_value": "",
            "env_rotate": "FOO",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    box = ProfileSecretBox("t")
    with Session(fresh_engine) as s:
        p = s.get(Profile, pid)
        assert box.decrypt(p.env) == {"FOO": "secret-foo"}


async def test_save_as_new_copies_env(cookie_client, fresh_client, fresh_engine):
    """Regression: 'Save as new' (UI duplicate) carries env vars to the copy.

    The form leaves secret env values blank, so without preservation the copy
    would land with an empty env blob.
    """
    from nightdesk.domain.profile_secrets import ProfileSecretBox

    create_r = await fresh_client.post("/api/v1/profiles", json={
        "name": "dup-src",
        "claude_credentials": {"source": "inherit"},
        "env": {"FOO": "secret-foo"},
    })
    pid = create_r.json()["id"]

    r = await cookie_client.post(
        f"/profiles/{pid}",
        data={
            "name": "dup-src",
            "save_as_new": "1",
            "save_as_new_name": "dup-copy",
            "env_field_submitted": "1",
            "env_name": "FOO",
            "env_value": "",
            "env_rotate": "FOO",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    box = ProfileSecretBox("t")
    with Session(fresh_engine) as s:
        copy = next(p for p in s.query(Profile).all() if p.name == "dup-copy")
        assert copy.env is not None
        assert box.decrypt(copy.env) == {"FOO": "secret-foo"}


async def test_edit_save_new_env_value_still_rotates(
    cookie_client, fresh_client, fresh_engine,
):
    """A non-empty value in a rotate row still replaces the stored value."""
    from nightdesk.domain.profile_secrets import ProfileSecretBox

    create_r = await fresh_client.post("/api/v1/profiles", json={
        "name": "rotate-env",
        "claude_credentials": {"source": "inherit"},
        "env": {"FOO": "old"},
    })
    pid = create_r.json()["id"]

    r = await cookie_client.post(
        f"/profiles/{pid}",
        data={
            "name": "rotate-env",
            "env_field_submitted": "1",
            "env_name": "FOO",
            "env_value": "new",
            "env_rotate": "FOO",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    box = ProfileSecretBox("t")
    with Session(fresh_engine) as s:
        p = s.get(Profile, pid)
        assert box.decrypt(p.env) == {"FOO": "new"}


# ---------------------------------------------------------------------------
# Auth-type token toggle (Bug 14). Pure JS behavior is hard to unit test, so
# these assert the markup hooks and the toggle logic the JS depends on. The
# behavior itself was verified by reading: ndUpdateCredentialField flips the
# input's `disabled` attribute and the row's opacity/pointer-events classes
# off the checked credentials_source radio, runs once on initial load, and
# fires onchange for each radio.
# ---------------------------------------------------------------------------


async def _edit_body(cookie_client, fresh_engine, name="Read only") -> str:
    from nightdesk.domain.profiles import list_profiles, seed_default_profiles

    seed_default_profiles(fresh_engine)
    with Session(fresh_engine) as s:
        pid = next(p.id for p in list_profiles(s) if p.name == name)
    r = await cookie_client.get(f"/profiles/{pid}/edit")
    assert r.status_code == 200
    return r.text


async def test_auth_token_toggle_wiring_present(cookie_client, fresh_engine):
    body = await _edit_body(cookie_client, fresh_engine)
    # The toggle target carries a stable hook and the radios call the handler.
    assert "data-cred-toggle" in body
    assert 'window.ndUpdateCredentialField && window.ndUpdateCredentialField(this.form)' in body
    # The handler must flip the disabled attribute (the load-bearing fix) and
    # the dimming classes, not just a hidden class.
    assert "input.disabled = false" in body
    assert "input.disabled = true" in body
    assert "row.classList.remove('opacity-50', 'pointer-events-none')" in body
    assert "row.classList.add('opacity-50', 'pointer-events-none')" in body
    # It distinguishes token-requiring types from inherit.
    assert "src !== 'inherit'" in body
    # And it runs on initial load reflecting the saved/checked radio.
    assert 'input[name="credentials_source"]:checked' in body


async def test_inherit_profile_renders_token_field_disabled(cookie_client, fresh_engine):
    """A seeded profile defaults to inherit, so the token input ships disabled."""
    body = await _edit_body(cookie_client, fresh_engine)
    # The credentials value input is present and rendered disabled for inherit.
    assert 'name="credentials_value"' in body
    # Server-side render keeps the input disabled when the source is inherit;
    # the JS keeps it in sync after that.
    row_start = body.index('id="credentials-value-row"')
    row_end = body.index("</div>", body.index("credentials_value", row_start))
    row = body[row_start:row_end]
    assert "disabled" in row
    assert "opacity-50 pointer-events-none" in body[row_start - 200:row_start + 200]


# ---------------------------------------------------------------------------
# Single model field (Bug 15). The auth section must not carry a model input;
# the only model selector is the dedicated Models fieldset, and the form must
# submit exactly one model-name value to the backend.
# ---------------------------------------------------------------------------


async def test_auth_section_has_no_model_field(cookie_client, fresh_engine):
    body = await _edit_body(cookie_client, fresh_engine)
    # Isolate the Authentication fieldset (legend "Authentication" up to the
    # next fieldset/section) and assert it carries no model input.
    auth_start = body.index("Authentication")
    auth_end = body.index("Permission mode", auth_start)
    auth = body[auth_start:auth_end]
    assert 'name="default_model"' not in auth
    assert "promoted_model_" not in auth
    # No stray model input named simply "model" anywhere either.
    assert 'name="model"' not in body


async def test_exactly_one_default_model_input(cookie_client, fresh_engine):
    body = await _edit_body(cookie_client, fresh_engine)
    assert body.count('name="default_model"') == 1


async def test_default_model_persists_as_single_value(
    cookie_client, fresh_client, fresh_engine,
):
    """Saving the form sends one model value through to the column."""
    create_r = await fresh_client.post("/api/v1/profiles", json={
        "name": "one-model",
        "claude_credentials": {"source": "inherit"},
    })
    pid = create_r.json()["id"]
    r = await cookie_client.post(
        f"/profiles/{pid}",
        data={
            "name": "one-model",
            "backend": "claude_sdk",
            "permission_mode": "default",
            "network_mode": "on",
            "default_model": "claude-sonnet-4-5",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with Session(fresh_engine) as s:
        p = s.get(Profile, pid)
        assert p.default_model == "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# Import from Claude Code settings.json (Bug 16). CC uses a different schema
# than Nightdesk's own export; translate_cc_settings maps it onto our fields.
# ---------------------------------------------------------------------------


_CC_SETTINGS_SAMPLE = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "model": "claude-opus-4-5",
    "env": {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        # Auth-owned keys must be stripped, never written to the env blob.
        "ANTHROPIC_API_KEY": "sk-ant-should-be-dropped",
        "ANTHROPIC_BASE_URL": "https://proxy.example",
    },
    "permissions": {
        "allow": ["Bash(npm run lint)", "Read(~/.zshrc)"],
        "deny": ["Bash(curl *)", "Read(./.env)"],
        "defaultMode": "acceptEdits",
        "additionalDirectories": ["../shared"],
    },
    "apiKeyHelper": "/bin/get-key.sh",
    "companyAnnouncements": ["hello"],
    # Forbidden keys: stripped with the same safety as native imports.
    "hooks": {"PostToolUse": []},
    "mcpServers": {"foo": {"command": "x"}},
}


def test_translate_cc_settings_maps_core_fields():
    from nightdesk.domain.profiles import translate_cc_settings

    fields = translate_cc_settings(_CC_SETTINGS_SAMPLE, name="from-cc")
    assert fields["name"] == "from-cc"
    assert fields["default_model"] == "claude-opus-4-5"
    assert fields["allowed_tools"] == ["Bash(npm run lint)", "Read(~/.zshrc)"]
    assert fields["denied_tools"] == ["Bash(curl *)", "Read(./.env)"]
    assert fields["permission_mode"] == "acceptEdits"
    # env keeps non-auth vars and drops the auth-owned ones.
    assert fields["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert fields["env"]["OTEL_METRICS_EXPORTER"] == "otlp"
    assert "ANTHROPIC_API_KEY" not in fields["env"]
    assert "ANTHROPIC_BASE_URL" not in fields["env"]


def test_translate_cc_settings_passthrough_and_strip():
    from nightdesk.domain.profiles import translate_cc_settings

    fields = translate_cc_settings(_CC_SETTINGS_SAMPLE, name="from-cc")
    pt = fields["cc_settings_passthrough"]
    # Unmapped top-level keys survive verbatim.
    assert pt["apiKeyHelper"] == "/bin/get-key.sh"
    assert pt["companyAnnouncements"] == ["hello"]
    # Unmapped permission sub-keys survive under permissions.
    assert pt["permissions"]["additionalDirectories"] == ["../shared"]
    # allow/deny/defaultMode were consumed, not duplicated into passthrough.
    assert "allow" not in pt["permissions"]
    assert "deny" not in pt["permissions"]
    assert "defaultMode" not in pt["permissions"]
    # Forbidden keys never reach the profile, not even in passthrough.
    assert "hooks" not in pt
    assert "mcpServers" not in pt
    assert "$schema" in pt  # benign, preserved


def test_translate_cc_settings_never_persists_auth_keys_anywhere():
    """Auth secrets must never land in a profile field — not in env, and not in
    the UNENCRYPTED cc_settings_passthrough column. Covers both the nested-env
    path and a top-level placement (e.g. a malformed file)."""
    from nightdesk.domain.profiles import translate_cc_settings

    fields = translate_cc_settings({
        "model": "claude-opus-4-7",
        # Top-level auth keys (not under env) — must be dropped, not parked.
        "ANTHROPIC_API_KEY": "sk-leak-top",
        "ANTHROPIC_BASE_URL": "https://evil.example",
        "env": {"ANTHROPIC_AUTH_TOKEN": "tok-leak-env", "FOO": "bar"},
    })
    pt = fields.get("cc_settings_passthrough", {})
    env = fields.get("env", {})
    for secret_key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        assert secret_key not in pt
        assert secret_key not in env
    # Non-secret env still imported.
    assert env.get("FOO") == "bar"
    assert fields["default_model"] == "claude-opus-4-7"


def test_translate_cc_settings_drops_unsupported_permission_mode():
    from nightdesk.domain.profiles import translate_cc_settings

    fields = translate_cc_settings({"permissions": {"defaultMode": "plan"}})
    # Unsupported mode is not applied; it lands in passthrough instead.
    assert "permission_mode" not in fields
    assert fields["cc_settings_passthrough"]["permissions"]["defaultMode"] == "plan"


def test_translate_cc_settings_rejects_non_object():
    from nightdesk.domain.profiles import translate_cc_settings

    with pytest.raises(ValueError):
        translate_cc_settings(["not", "a", "dict"])


async def test_import_cc_route_creates_profile(cookie_client, fresh_engine):
    files = {"file": ("settings.json",
                       io.BytesIO(json.dumps(_CC_SETTINGS_SAMPLE).encode()),
                       "application/json")}
    r = await cookie_client.post(
        "/profiles/import-cc",
        files=files,
        data={"name": "cc-imported"},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body["dropped_fields"]) >= {"hooks", "mcpServers"}

    from nightdesk.domain.profile_secrets import ProfileSecretBox
    box = ProfileSecretBox("t")
    with Session(fresh_engine) as s:
        p = s.get(Profile, body["id"])
        assert p.name == "cc-imported"
        assert p.default_model == "claude-opus-4-5"
        assert p.permission_mode == "acceptEdits"
        assert p.allowed_tools == ["Bash(npm run lint)", "Read(~/.zshrc)"]
        assert p.denied_tools == ["Bash(curl *)", "Read(./.env)"]
        assert p.cc_settings_passthrough["apiKeyHelper"] == "/bin/get-key.sh"
        # Auth secret never persisted into the env blob.
        decoded = box.decrypt(p.env)
        assert "ANTHROPIC_API_KEY" not in decoded
        assert decoded["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"


async def test_import_cc_route_rejects_bad_json(cookie_client):
    files = {"file": ("settings.json", io.BytesIO(b"not json"),
                       "application/json")}
    r = await cookie_client.post("/profiles/import-cc", files=files)
    assert r.status_code == 400


async def test_profiles_page_offers_cc_import_affordance(cookie_client):
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert 'action="/profiles/import-cc"' in r.text
    assert "Import from Claude Code settings.json" in r.text


# ---------------------------------------------------------------------------
# Backend-aware profile forms. The editor renders per-backend field groups via
# the capability descriptors: shared sandbox/runtime fields always show, while
# Claude- and OMP/RPC-specific sections are gated by the selected backend.
# ---------------------------------------------------------------------------


async def test_new_form_renders_both_backend_sections_and_toggle(cookie_client):
    """Both backend-specific sections are present in the markup (so the JS can
    flip between them) and the toggle wiring ships with the form."""
    r = await cookie_client.get("/profiles/new")
    body = r.text
    # Claude section appears twice (auth/binary/permission, then models/behavior),
    # OMP once.
    assert body.count('data-backend-section="claude_sdk"') == 2
    assert body.count('data-backend-section="omp_rpc"') == 1
    # OMP-specific inputs live in the OMP section, stored in the env blob.
    assert 'name="promoted_omp_endpoint"' in body
    assert 'name="promoted_omp_auth_token"' in body
    assert 'name="promoted_omp_model"' in body
    # The toggle JS and its entrypoint are present.
    assert "ndApplyBackend" in body
    assert 'select[name="backend"]' in body


async def test_new_form_hides_omp_section_by_default(cookie_client):
    """A new profile defaults to claude_sdk: the OMP section ships hidden and
    the Claude sections visible (initial server render avoids a flash)."""
    r = await cookie_client.get("/profiles/new")
    body = r.text
    # The omp section wrapper carries the hidden class on initial render.
    omp_idx = body.index('data-backend-section="omp_rpc"')
    # Grab the opening tag of that wrapper.
    tag = body[omp_idx - 40:body.index(">", omp_idx)]
    assert "hidden" in tag
    # The claude wrapper is NOT hidden by default.
    claude_idx = body.index('data-backend-section="claude_sdk"')
    claude_tag = body[claude_idx - 40:body.index(">", claude_idx)]
    assert "hidden" not in claude_tag


async def test_omp_profile_edit_shows_omp_section_visible(
    cookie_client, fresh_client, fresh_engine,
):
    """Editing an omp_rpc profile renders the OMP section visible and the
    Claude sections hidden on the server side."""
    r = await fresh_client.post("/api/v1/profiles", json={
        "name": "omp-edit", "backend": "omp_rpc",
        "claude_credentials": {"source": "inherit"},
    })
    pid = r.json()["id"]
    r = await cookie_client.get(f"/profiles/{pid}/edit")
    body = r.text
    # OMP wrapper visible, claude wrapper hidden.
    omp_idx = body.index('data-backend-section="omp_rpc"')
    omp_tag = body[omp_idx - 40:body.index(">", omp_idx)]
    assert "hidden" not in omp_tag
    claude_idx = body.index('data-backend-section="claude_sdk"')
    claude_tag = body[claude_idx - 40:body.index(">", claude_idx)]
    assert "hidden" in claude_tag
    # The backend dropdown has omp_rpc selected.
    assert 'value="omp_rpc"' in body


async def test_create_omp_profile_persists_connection_to_env(
    cookie_client, fresh_engine,
):
    """Posting the OMP fieldset writes endpoint/model/token into the encrypted
    env blob and keeps the token out of plaintext."""
    from nightdesk.domain.profile_secrets import ProfileSecretBox

    r = await cookie_client.post(
        "/profiles",
        data={
            "name": "omp-create",
            "backend": "omp_rpc",
            "network_mode": "on",
            "promoted_env_submitted": "1",
            "promoted_omp_endpoint": "https://omp.example/rpc",
            "promoted_omp_auth_token": "rpc-secret-789",
            "promoted_omp_model": "gpt-oss-120b",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    pid = r.headers["location"].rsplit("/", 1)[-1]
    box = ProfileSecretBox("t")
    with Session(fresh_engine) as s:
        p = s.get(Profile, pid)
        assert p.backend == "omp_rpc"
        decoded = box.decrypt(p.env)
        assert decoded["OMP_RPC_ENDPOINT"] == "https://omp.example/rpc"
        assert decoded["OMP_RPC_MODEL"] == "gpt-oss-120b"
        assert decoded["OMP_RPC_AUTH_TOKEN"] == "rpc-secret-789"
        # Secret never lands in plaintext on the row.
        assert "rpc-secret-789" not in (p.env or "")


async def test_omp_blank_token_keeps_existing(cookie_client, fresh_engine):
    """Saving the OMP form with a blank token preserves the stored value
    (mirrors the credential 'leave blank to keep' contract)."""
    from nightdesk.domain.profile_secrets import ProfileSecretBox

    r = await cookie_client.post(
        "/profiles",
        data={
            "name": "omp-keep-token", "backend": "omp_rpc", "network_mode": "on",
            "promoted_env_submitted": "1",
            "promoted_omp_endpoint": "https://e/rpc",
            "promoted_omp_auth_token": "tok-orig",
            "promoted_omp_model": "m",
        },
        follow_redirects=False,
    )
    pid = r.headers["location"].rsplit("/", 1)[-1]
    # Re-save with the token blank but the endpoint changed.
    r = await cookie_client.post(
        f"/profiles/{pid}",
        data={
            "name": "omp-keep-token", "backend": "omp_rpc", "network_mode": "on",
            "promoted_env_submitted": "1",
            "promoted_omp_endpoint": "https://e2/rpc",
            "promoted_omp_auth_token": "",
            "promoted_omp_model": "m",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    box = ProfileSecretBox("t")
    with Session(fresh_engine) as s:
        decoded = box.decrypt(s.get(Profile, pid).env)
    assert decoded["OMP_RPC_AUTH_TOKEN"] == "tok-orig"
    assert decoded["OMP_RPC_ENDPOINT"] == "https://e2/rpc"


async def test_switching_backend_preserves_other_backend_keys(
    cookie_client, fresh_engine,
):
    """Switching an OMP profile to claude_sdk and saving must not wipe the OMP
    connection keys: the inactive backend's promoted keys are preserved so a
    user inspecting under another backend doesn't lose config."""
    from nightdesk.domain.profile_secrets import ProfileSecretBox

    r = await cookie_client.post(
        "/profiles",
        data={
            "name": "omp-switch", "backend": "omp_rpc", "network_mode": "on",
            "promoted_env_submitted": "1",
            "promoted_omp_endpoint": "https://e/rpc",
            "promoted_omp_auth_token": "tok-X",
            "promoted_omp_model": "m1",
        },
        follow_redirects=False,
    )
    pid = r.headers["location"].rsplit("/", 1)[-1]
    # Save as claude_sdk with a claude model set; OMP fields are absent (their
    # fieldset is disabled on the client when claude is selected).
    r = await cookie_client.post(
        f"/profiles/{pid}",
        data={
            "name": "omp-switch", "backend": "claude_sdk", "network_mode": "on",
            "permission_mode": "default",
            "credentials_field_submitted": "1", "credentials_source": "inherit",
            "promoted_env_submitted": "1",
            "promoted_model_ANTHROPIC_MODEL": "claude-opus-4-5",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    box = ProfileSecretBox("t")
    with Session(fresh_engine) as s:
        decoded = box.decrypt(s.get(Profile, pid).env)
    assert decoded["ANTHROPIC_MODEL"] == "claude-opus-4-5"
    assert decoded["OMP_RPC_AUTH_TOKEN"] == "tok-X"
    assert decoded["OMP_RPC_ENDPOINT"] == "https://e/rpc"


async def test_view_preview_shows_backend_capabilities_for_claude(
    cookie_client, fresh_engine,
):
    """The effective-config preview lists shared/specific/inert capability
    groups. For Claude, OMP connection shows up as inert."""
    seed_default_profiles(fresh_engine)
    with Session(fresh_engine) as s:
        pid = next(p.id for p in list_profiles(s) if p.name == "Read only")
    r = await cookie_client.get(f"/profiles/{pid}")
    body = r.text
    assert "Backend capabilities" in body
    assert "Not used (inert)" in body
    # Claude-specific groups render under the "<label>-specific" column.
    assert "Claude Code-specific" in body
    # OMP connection is the inert group for a Claude profile.
    assert "OMP / RPC connection" in body


async def test_view_preview_for_omp_redacts_token_and_hides_claude_rows(
    cookie_client, fresh_client, fresh_engine,
):
    """The OMP preview surfaces endpoint/model + 'value set' for the token (no
    plaintext), and the Claude-only rows (Credentials/Permission mode) are
    absent because they're inert for this backend."""
    r = await fresh_client.post("/api/v1/profiles", json={
        "name": "omp-view", "backend": "omp_rpc",
        "claude_credentials": {"source": "inherit"},
        "env": {
            "OMP_RPC_ENDPOINT": "https://omp.view/rpc",
            "OMP_RPC_MODEL": "gpt-oss-120b",
            "OMP_RPC_AUTH_TOKEN": "view-token-leak",
        },
    })
    pid = r.json()["id"]
    r = await cookie_client.get(f"/profiles/{pid}")
    body = r.text
    # Endpoint + model visible; token redacted.
    assert "https://omp.view/rpc" in body
    assert "gpt-oss-120b" in body
    assert "view-token-leak" not in body
    # Claude-only effective-config rows do not appear for an OMP profile.
    assert "Credentials" not in body
    # Capability panel marks OMP/RPC connection as the active backend-specific
    # group, and Claude groups as inert.
    assert "OMP / RPC-specific" in body
    assert "Authentication" in body  # listed as an inert group


async def test_form_create_accepts_omp_backend(cookie_client, fresh_engine):
    """omp_rpc is a wired backend now, so the form POST accepts it."""
    r = await cookie_client.post(
        "/profiles",
        data={"name": "omp-ok", "backend": "omp_rpc", "network_mode": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


# ---------------------------------------------------------------------------
# JSON API: backend-conditional credentials (Finding 2).
# ---------------------------------------------------------------------------


async def test_json_create_omp_rpc_without_credentials_succeeds(fresh_client):
    """omp_rpc profiles do not use Claude credentials, so creating one without
    claude_credentials must return 201 rather than a 400 validation error."""
    r = await fresh_client.post("/api/v1/profiles", json={
        "name": "omp-no-creds",
        "backend": "omp_rpc",
    })
    assert r.status_code == 201, r.text
    assert r.json()["backend"] == "omp_rpc"


async def test_json_create_claude_sdk_without_credentials_400s(fresh_client):
    """claude_sdk profiles still require claude_credentials; omitting them
    must 400 as before."""
    r = await fresh_client.post("/api/v1/profiles", json={
        "name": "sdk-no-creds",
        "backend": "claude_sdk",
    })
    assert r.status_code == 400, r.text
    assert "claude_credentials" in r.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# omp_rpc not-executable warning (Finding 3).
# ---------------------------------------------------------------------------


async def test_view_omp_profile_shows_not_executable_warning(
    cookie_client, fresh_client, fresh_engine,
):
    """The view pane for an omp_rpc profile must show a clear warning that
    runs will fail, driven by the backend_capability.executable flag."""
    r = await fresh_client.post("/api/v1/profiles", json={
        "name": "omp-exec-warn", "backend": "omp_rpc",
    })
    pid = r.json()["id"]
    r = await cookie_client.get(f"/profiles/{pid}")
    assert r.status_code == 200
    assert "not yet executable" in r.text.lower()
    assert "runs will fail" in r.text.lower()


async def test_form_omp_section_shows_not_executable_warning(cookie_client):
    """The edit form's OMP/RPC section carries the not-executable warning so
    it appears when the user switches to omp_rpc, driven by the capability
    registry rather than a hardcoded backend-name check in the template."""
    r = await cookie_client.get("/profiles/new")
    assert r.status_code == 200
    body = r.text
    # Warning text must be in the page (inside the omp_rpc section).
    assert "not yet executable" in body.lower()
    assert "runs will fail" in body.lower()


async def test_claude_profile_has_no_not_executable_warning(
    cookie_client, fresh_engine,
):
    """A claude_sdk profile must not show the not-executable warning."""
    seed_default_profiles(fresh_engine)
    with Session(fresh_engine) as s:
        pid = next(p.id for p in list_profiles(s) if p.name == "Read only")
    r = await cookie_client.get(f"/profiles/{pid}")
    assert r.status_code == 200
    # The warning div must not appear for an executable backend.
    assert "not yet executable" not in r.text.lower()


# ---------------------------------------------------------------------------
# secret_keys form field (Finding 4).
# ---------------------------------------------------------------------------


async def test_form_renders_secret_keys_field(cookie_client):
    """The edit form must expose a secret_keys textarea so users can mark
    env variables as secrets that should never appear in logs."""
    r = await cookie_client.get("/profiles/new")
    assert r.status_code == 200
    assert 'name="secret_keys"' in r.text


async def test_secret_keys_persists_through_form(
    cookie_client, fresh_engine,
):
    """Submitting secret_keys via the form persists them to Profile.secret_keys."""
    r = await cookie_client.post(
        "/profiles",
        data={
            "name": "sk-test",
            "backend": "claude_sdk",
            "network_mode": "on",
            "secret_keys": "MY_TOKEN\nDB_PASS",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    pid = r.headers["location"].rsplit("/", 1)[-1]
    with Session(fresh_engine) as s:
        p = s.get(Profile, pid)
    assert set(p.secret_keys) == {"MY_TOKEN", "DB_PASS"}
