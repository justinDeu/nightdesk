"""Tests for the Provider + ProviderEndpoint domain layer.

Covers CRUD, per-credential-source resolution, the compatibility gate
(including harness_lock), both deletion-guard paths (direct FK reference and
a per-agent reference buried in backend_config JSON), and catalog shape.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from nightdesk.db.models import Profile, Provider, ProviderEndpoint
from nightdesk.domain import provider_catalog
from nightdesk.domain import providers as providers_domain
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.providers import (
    EndpointInUse,
    EndpointNotFound,
    ProviderInUse,
    ProviderNameTaken,
    ProviderNotFound,
    ResolvedEndpoint,
    UnknownCredentialSource,
    UnknownProtocolKind,
    create_endpoint,
    create_provider,
    delete_endpoint,
    delete_provider,
    endpoint_compatible,
    get_endpoint,
    get_provider,
    list_endpoints,
    list_providers,
    resolve_endpoint,
    resolve_endpoints_for_profile,
    update_endpoint,
    update_provider,
)


@pytest.fixture
def box() -> ProfileSecretBox:
    return ProfileSecretBox("test-bearer-token")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_and_get_provider(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    assert p.id
    fetched = get_provider(session, p.id)
    assert fetched.name == "ZAI"
    assert fetched.vendor == "zai"


def test_provider_name_must_be_unique(session):
    create_provider(session, name="ZAI", vendor="zai")
    with pytest.raises(ProviderNameTaken):
        create_provider(session, name="ZAI", vendor="zai")


def test_get_missing_provider_raises(session):
    with pytest.raises(ProviderNotFound):
        get_provider(session, "nope")


def test_list_providers_ordered_by_name(session):
    create_provider(session, name="ZAI", vendor="zai")
    create_provider(session, name="Anthropic", vendor="anthropic")
    names = [p.name for p in list_providers(session)]
    assert names == ["Anthropic", "ZAI"]


def test_update_provider(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    updated = update_provider(session, p.id, name="ZAI Team A")
    assert updated.name == "ZAI Team A"


def test_create_endpoint_under_provider(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session,
        provider_id=p.id,
        label="Anthropic-compatible",
        protocol_kind="anthropic_compat",
        base_url="https://api.z.ai/api/anthropic",
        credential_source="api_key",
    )
    assert ep.provider_id == p.id
    fetched = get_endpoint(session, ep.id)
    assert fetched.protocol_kind == "anthropic_compat"


def test_create_endpoint_unknown_protocol_kind_rejected(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    with pytest.raises(UnknownProtocolKind):
        create_endpoint(session, provider_id=p.id, protocol_kind="bogus")


def test_create_endpoint_unknown_credential_source_rejected(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    with pytest.raises(UnknownCredentialSource):
        create_endpoint(
            session, provider_id=p.id, protocol_kind="anthropic_compat",
            credential_source="bogus",
        )


def test_create_endpoint_missing_provider_raises(session):
    with pytest.raises(ProviderNotFound):
        create_endpoint(session, provider_id="nope", protocol_kind="anthropic_compat")


def test_no_unique_constraint_on_provider_and_protocol(session):
    """Real vendors expose two surfaces on one protocol; must not collide."""
    p = create_provider(session, name="ZAI", vendor="zai")
    create_endpoint(session, provider_id=p.id, label="pay-as-you-go",
                     protocol_kind="anthropic_compat")
    create_endpoint(session, provider_id=p.id, label="coding-plan",
                     protocol_kind="anthropic_compat")
    assert len(list_endpoints(session, p.id)) == 2


def test_update_endpoint(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(session, provider_id=p.id, protocol_kind="anthropic_compat")
    updated = update_endpoint(session, ep.id, label="renamed")
    assert updated.label == "renamed"


def test_get_missing_endpoint_raises(session):
    with pytest.raises(EndpointNotFound):
        get_endpoint(session, "nope")


# ---------------------------------------------------------------------------
# Credential resolution, per source
# ---------------------------------------------------------------------------


def test_resolve_api_key_credential(session, box):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session, provider_id=p.id, label="ep", protocol_kind="anthropic_compat",
        base_url="https://api.z.ai", credential_source="api_key",
        credential=box.encrypt("secret-key-123"),
        default_model="glm-5.2", models=["glm-5.2", "glm-4.5"],
    )
    resolved = resolve_endpoint(session, ep.id, box)
    assert isinstance(resolved, ResolvedEndpoint)
    assert resolved.credential == "secret-key-123"
    assert resolved.credential_source == "api_key"
    assert resolved.protocol_kind == "anthropic_compat"
    assert resolved.base_url == "https://api.z.ai"
    assert resolved.default_model == "glm-5.2"
    assert resolved.models == ["glm-5.2", "glm-4.5"]
    assert resolved.provider_id == p.id
    assert resolved.provider_name == "ZAI"
    assert resolved.vendor == "zai"


def test_resolve_env_var_credential(session, box, monkeypatch):
    monkeypatch.setenv("MY_ZAI_KEY", "env-secret-456")
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session, provider_id=p.id, protocol_kind="anthropic_compat",
        credential_source="env_var", credential=box.encrypt("MY_ZAI_KEY"),
    )
    resolved = resolve_endpoint(session, ep.id, box)
    assert resolved.credential == "env-secret-456"


def test_resolve_env_var_missing_logs_and_returns_none(session, box, monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session, provider_id=p.id, protocol_kind="anthropic_compat",
        credential_source="env_var", credential=box.encrypt("MISSING_VAR"),
    )
    resolved = resolve_endpoint(session, ep.id, box)
    assert resolved.credential is None


def test_resolve_oauth_file_credential(session, box, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"access_token": "tok123"}')
    p = create_provider(session, name="Codex", vendor="openai")
    ep = create_endpoint(
        session, provider_id=p.id, protocol_kind="openai_codex",
        credential_source="oauth_file", credential=box.encrypt(str(auth_file)),
    )
    resolved = resolve_endpoint(session, ep.id, box)
    assert resolved.credential == '{"access_token": "tok123"}'


def test_resolve_subscription_file_credential(session, box, tmp_path):
    creds_file = tmp_path / "creds.json"
    creds_file.write_text("host-auth-blob")
    p = create_provider(session, name="Anthropic", vendor="anthropic")
    ep = create_endpoint(
        session, provider_id=p.id, protocol_kind="anthropic",
        credential_source="subscription_file", credential=box.encrypt(str(creds_file)),
        harness_lock="claude_sdk",
    )
    resolved = resolve_endpoint(session, ep.id, box)
    assert resolved.credential == "host-auth-blob"


def test_resolve_file_credential_missing_file_returns_none(session, box, tmp_path):
    missing = tmp_path / "gone.json"
    p = create_provider(session, name="Codex", vendor="openai")
    ep = create_endpoint(
        session, provider_id=p.id, protocol_kind="openai_codex",
        credential_source="oauth_file", credential=box.encrypt(str(missing)),
    )
    resolved = resolve_endpoint(session, ep.id, box)
    assert resolved.credential is None


def test_resolve_none_credential_source(session, box):
    p = create_provider(session, name="Ollama", vendor="ollama")
    ep = create_endpoint(
        session, provider_id=p.id, protocol_kind="ollama",
        base_url="http://localhost:11434/v1", credential_source="none",
    )
    resolved = resolve_endpoint(session, ep.id, box)
    assert resolved.credential is None


def test_resolve_decrypt_failure_returns_none_not_raises(session, tmp_path):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session, provider_id=p.id, protocol_kind="anthropic_compat",
        credential_source="api_key", credential="not-a-valid-fernet-token",
    )
    box_a = ProfileSecretBox("bearer-a")
    resolved = resolve_endpoint(session, ep.id, box_a)
    assert resolved is not None
    assert resolved.credential is None


def test_resolve_missing_endpoint_id_returns_none(session, box):
    assert resolve_endpoint(session, None, box) is None
    assert resolve_endpoint(session, "nope", box) is None


def test_resolve_extra_decrypts_to_dict(session, box):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session, provider_id=p.id, protocol_kind="openai_compat",
        extra=box.encrypt({"headers": {"X-Routing": "team-a"}}),
    )
    resolved = resolve_endpoint(session, ep.id, box)
    assert resolved.extra == {"headers": {"X-Routing": "team-a"}}


def test_resolve_extra_absent_or_bad_defaults_to_empty_dict(session, box):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(session, provider_id=p.id, protocol_kind="openai_compat")
    resolved = resolve_endpoint(session, ep.id, box)
    assert resolved.extra == {}


# ---------------------------------------------------------------------------
# resolve_endpoints_for_profile
# ---------------------------------------------------------------------------


def test_resolve_endpoints_for_profile_primary_and_agents(session, box):
    p = create_provider(session, name="ZAI", vendor="zai")
    primary = create_endpoint(session, provider_id=p.id, protocol_kind="openai_codex")
    agent_ep = create_endpoint(session, provider_id=p.id, protocol_kind="openai_compat")
    profile = Profile(
        name="mixed", endpoint_id=primary.id,
        backend_config={"agents": [{"name": "researcher", "endpoint_id": agent_ep.id}]},
    )
    session.add(profile)
    session.commit()
    resolved = resolve_endpoints_for_profile(session, profile, box)
    assert set(resolved.keys()) == {primary.id, agent_ep.id}


def test_resolve_endpoints_for_profile_dedupes_shared_endpoint(session, box):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(session, provider_id=p.id, protocol_kind="openai_compat")
    profile = Profile(
        name="shared", endpoint_id=ep.id,
        backend_config={"agents": [{"name": "researcher", "endpoint_id": ep.id}]},
    )
    session.add(profile)
    session.commit()
    resolved = resolve_endpoints_for_profile(session, profile, box)
    assert list(resolved.keys()) == [ep.id]


# ---------------------------------------------------------------------------
# Compatibility gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeCapability:
    code: str
    protocol_kinds: frozenset


@dataclass(frozen=True)
class _FakeCapabilityOldSpelling:
    """Mirrors a not-yet-renamed BackendCapability (provider_kinds)."""
    code: str
    provider_kinds: frozenset


def test_endpoint_compatible_protocol_intersection(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(session, provider_id=p.id, protocol_kind="anthropic_compat")
    cap = _FakeCapability(code="claude_sdk", protocol_kinds=frozenset({"anthropic", "anthropic_compat"}))
    assert endpoint_compatible(cap, ep)

    other_cap = _FakeCapability(code="opencode", protocol_kinds=frozenset({"openai_codex"}))
    assert not endpoint_compatible(other_cap, ep)


def test_endpoint_compatible_reads_old_spelling(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(session, provider_id=p.id, protocol_kind="anthropic_compat")
    cap = _FakeCapabilityOldSpelling(code="claude_sdk", provider_kinds=frozenset({"anthropic_compat"}))
    assert endpoint_compatible(cap, ep)


def test_endpoint_compatible_harness_lock_blocks_other_harnesses(session):
    p = create_provider(session, name="Anthropic", vendor="anthropic")
    ep = create_endpoint(
        session, provider_id=p.id, protocol_kind="anthropic",
        credential_source="subscription_file", harness_lock="claude_sdk",
    )
    claude_cap = _FakeCapability(code="claude_sdk", protocol_kinds=frozenset({"anthropic", "anthropic_compat"}))
    opencode_cap = _FakeCapability(code="opencode", protocol_kinds=frozenset({"anthropic", "anthropic_compat"}))
    assert endpoint_compatible(claude_cap, ep)
    assert not endpoint_compatible(opencode_cap, ep)


def test_endpoint_compatible_on_resolved_endpoint(session, box):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(session, provider_id=p.id, protocol_kind="openai_compat")
    resolved = resolve_endpoint(session, ep.id, box)
    cap = _FakeCapability(code="opencode", protocol_kinds=frozenset({"openai_compat"}))
    assert endpoint_compatible(cap, resolved)


# ---------------------------------------------------------------------------
# Deletion guards
# ---------------------------------------------------------------------------


def test_delete_endpoint_blocked_by_direct_profile_reference(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(session, provider_id=p.id, protocol_kind="openai_compat")
    session.add(Profile(name="direct-ref", endpoint_id=ep.id))
    session.commit()
    with pytest.raises(EndpointInUse):
        delete_endpoint(session, ep.id)


def test_delete_endpoint_blocked_by_backend_config_agent_reference(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(session, provider_id=p.id, protocol_kind="openai_compat")
    session.add(Profile(
        name="agent-ref",
        backend_config={"agents": [{"name": "researcher", "endpoint_id": ep.id}]},
    ))
    session.commit()
    with pytest.raises(EndpointInUse):
        delete_endpoint(session, ep.id)


def test_delete_unreferenced_endpoint_succeeds(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(session, provider_id=p.id, protocol_kind="openai_compat")
    delete_endpoint(session, ep.id)
    with pytest.raises(EndpointNotFound):
        get_endpoint(session, ep.id)


def test_delete_provider_blocked_when_endpoint_in_use(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(session, provider_id=p.id, protocol_kind="openai_compat")
    session.add(Profile(name="direct-ref", endpoint_id=ep.id))
    session.commit()
    with pytest.raises(ProviderInUse):
        delete_provider(session, p.id)


def test_delete_provider_cascades_unreferenced_endpoints(session):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep1 = create_endpoint(session, provider_id=p.id, protocol_kind="openai_compat")
    ep2 = create_endpoint(session, provider_id=p.id, protocol_kind="anthropic_compat")
    delete_provider(session, p.id)
    with pytest.raises(EndpointNotFound):
        get_endpoint(session, ep1.id)
    with pytest.raises(EndpointNotFound):
        get_endpoint(session, ep2.id)
    with pytest.raises(ProviderNotFound):
        get_provider(session, p.id)


# ---------------------------------------------------------------------------
# harness_lock carried onto ResolvedEndpoint
# ---------------------------------------------------------------------------


def test_resolve_endpoint_carries_harness_lock(session, box, tmp_path):
    creds_file = tmp_path / "creds.json"
    creds_file.write_text("blob")
    p = create_provider(session, name="Anthropic", vendor="anthropic")
    ep = create_endpoint(
        session, provider_id=p.id, protocol_kind="anthropic",
        credential_source="subscription_file", credential=box.encrypt(str(creds_file)),
        harness_lock="claude_sdk",
    )
    resolved = resolve_endpoint(session, ep.id, box)
    assert resolved.harness_lock == "claude_sdk"


def test_resolve_endpoint_harness_lock_defaults_to_none(session, box):
    p = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(session, provider_id=p.id, protocol_kind="anthropic_compat")
    resolved = resolve_endpoint(session, ep.id, box)
    assert resolved.harness_lock is None


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_catalog_shape():
    cat = provider_catalog.catalog()
    vendors = {v.vendor for v in cat}
    assert vendors == {"zai", "anthropic", "openai", "openrouter", "ollama"}
    for vendor in cat:
        assert vendor.endpoints, vendor.vendor
        for ep in vendor.endpoints:
            assert ep.protocol_kind in providers_domain.PROTOCOL_KINDS
            assert ep.credential_source in providers_domain.CREDENTIAL_SOURCES


def test_catalog_anthropic_subscription_locked_to_claude_sdk():
    anthropic = provider_catalog.get_vendor_template("anthropic")
    sub = [e for e in anthropic.endpoints if e.credential_source == "subscription_file"]
    assert len(sub) == 1
    assert sub[0].harness_lock == "claude_sdk"


def test_catalog_openai_codex_uses_oauth_file():
    openai = provider_catalog.get_vendor_template("openai")
    codex = [e for e in openai.endpoints if e.protocol_kind == "openai_codex"]
    assert len(codex) == 1
    assert codex[0].credential_source == "oauth_file"


def test_get_vendor_template_unknown_returns_none():
    assert provider_catalog.get_vendor_template("bogus") is None
