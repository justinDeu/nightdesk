"""Tests for the v1 profile JSON API (credential/env encryption, capability
listing, CC-settings translation) and profile seeding."""
from __future__ import annotations

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
# Env variable catalog.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Import from Claude Code settings.json. CC uses a different schema than
# Nightdesk's own export; translate_cc_settings maps it onto our fields.
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




# ---------------------------------------------------------------------------
# secret_keys form field (Finding 4).
# ---------------------------------------------------------------------------
