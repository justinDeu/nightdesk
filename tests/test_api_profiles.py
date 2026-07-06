async def test_create_list_get_update_delete_profile(client):
    payload = {
        "name": "p1",
        "fs_read": ["/tmp"],
        "fs_write": [],
        "allowed_tools": ["Read"],
        "denied_tools": [],
        "network_mode": "off",
        "network_allowlist": [],
        "secret_keys": [],
        "default_model": None,
        "claude_credentials": {"source": "inherit"},
    }
    r = await client.post("/api/v1/profiles", json=payload)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = await client.get("/api/v1/profiles")
    assert r.status_code == 200
    assert any(p["id"] == pid for p in r.json())

    r = await client.get(f"/api/v1/profiles/{pid}")
    assert r.status_code == 200

    r = await client.patch(f"/api/v1/profiles/{pid}", json={"network_mode": "on"})
    assert r.status_code == 200
    assert r.json()["network_mode"] == "on"

    r = await client.delete(f"/api/v1/profiles/{pid}")
    assert r.status_code == 204


async def _make_endpoint(client, *, protocol_kind, harness_lock=None, models=None, base_url=None):
    r = await client.post("/api/v1/providers", json={
        "name": f"prov-{protocol_kind}-{harness_lock}-{models}", "vendor": "custom",
        "endpoints": [{
            "label": "ep", "protocol_kind": protocol_kind, "harness_lock": harness_lock,
            "models": models or [], "base_url": base_url,
        }],
    })
    assert r.status_code == 201, r.text
    return r.json()["endpoints"][0]["id"]


async def test_create_profile_blocked_by_protocol_mismatch(client):
    # claude_sdk speaks {anthropic, anthropic_compat, claude_subscription}; an
    # openai-protocol endpoint is not in that set.
    eid = await _make_endpoint(client, protocol_kind="openai")
    r = await client.post("/api/v1/profiles", json={
        "name": "cc-bad-protocol",
        "backend": "claude_sdk",
        "endpoint_id": eid,
        "claude_credentials": {"source": "inherit"},
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
    })
    assert r.status_code == 400, r.text
    assert "not compatible" in r.json()["detail"] or "does not support" in r.json()["detail"]


async def test_create_profile_blocked_by_harness_lock_mismatch(client):
    # Protocol matches (anthropic), but the endpoint is locked to a different harness.
    eid = await _make_endpoint(client, protocol_kind="anthropic", harness_lock="opencode")
    r = await client.post("/api/v1/profiles", json={
        "name": "cc-locked-out",
        "backend": "claude_sdk",
        "endpoint_id": eid,
        "claude_credentials": {"source": "inherit"},
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
    })
    assert r.status_code == 400, r.text
    assert "locked" in r.json()["detail"]


async def test_create_profile_blocked_by_incompatible_agent_endpoint(client):
    # Primary endpoint is fine; the per-agent endpoint is protocol-mismatched.
    primary_eid = await _make_endpoint(client, protocol_kind="openai_compat")
    bad_agent_eid = await _make_endpoint(client, protocol_kind="anthropic", harness_lock="claude_sdk")
    r = await client.post("/api/v1/profiles", json={
        "name": "opencode-bad-agent",
        "backend": "opencode",
        "endpoint_id": primary_eid,
        "backend_config": {"agents": [{"name": "researcher", "endpoint_id": bad_agent_eid}]},
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
    })
    assert r.status_code == 400, r.text
    assert "locked" in r.json()["detail"]


async def test_create_profile_warns_but_saves_on_off_menu_model(client):
    eid = await _make_endpoint(client, protocol_kind="openai_compat", models=["glm-5.2"])
    r = await client.post("/api/v1/profiles", json={
        "name": "opencode-off-menu",
        "backend": "opencode",
        "endpoint_id": eid,
        "default_model": "not-on-the-menu",
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["default_model"] == "not-on-the-menu"
    assert any("not-on-the-menu" in w for w in body["warnings"])


async def test_create_profile_no_warnings_when_model_on_menu(client):
    eid = await _make_endpoint(client, protocol_kind="openai_compat", models=["glm-5.2"])
    r = await client.post("/api/v1/profiles", json={
        "name": "opencode-on-menu",
        "backend": "opencode",
        "endpoint_id": eid,
        "default_model": "glm-5.2",
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
    })
    assert r.status_code == 201, r.text
    assert r.json()["warnings"] == []


async def test_update_profile_backend_change_revalidates_endpoint(client):
    # openai_compat is fine for opencode but not for claude_sdk.
    eid = await _make_endpoint(client, protocol_kind="openai_compat")
    r = await client.post("/api/v1/profiles", json={
        "name": "switching-backend",
        "backend": "opencode",
        "endpoint_id": eid,
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
    })
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = await client.patch(f"/api/v1/profiles/{pid}", json={
        "backend": "claude_sdk",
        "claude_credentials": {"source": "inherit"},
    })
    assert r.status_code == 400, r.text


async def test_create_profile_unknown_endpoint_404(client):
    r = await client.post("/api/v1/profiles", json={
        "name": "unknown-endpoint",
        "backend": "opencode",
        "endpoint_id": "does-not-exist",
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
    })
    assert r.status_code == 404


async def test_create_claude_sdk_profile_with_endpoint_needs_no_credentials(client):
    # A ProviderEndpoint supplies authentication; claude_credentials is only
    # a fallback and must not be mandatory when endpoint_id is set.
    eid = await _make_endpoint(client, protocol_kind="anthropic")
    r = await client.post("/api/v1/profiles", json={
        "name": "cc-endpoint-no-creds",
        "backend": "claude_sdk",
        "endpoint_id": eid,
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
    })
    assert r.status_code == 201, r.text
    assert r.json()["claude_credentials"] is None


async def test_create_claude_sdk_profile_without_endpoint_still_requires_credentials(client):
    r = await client.post("/api/v1/profiles", json={
        "name": "cc-no-endpoint-no-creds",
        "backend": "claude_sdk",
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
    })
    assert r.status_code == 400, r.text
    assert "claude_credentials is required" in r.json()["detail"]


async def test_create_claude_sdk_profile_with_endpoint_and_credentials_keeps_both(client):
    eid = await _make_endpoint(client, protocol_kind="anthropic")
    r = await client.post("/api/v1/profiles", json={
        "name": "cc-endpoint-and-creds",
        "backend": "claude_sdk",
        "endpoint_id": eid,
        "claude_credentials": {"source": "inherit"},
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["endpoint_id"] == eid
    assert body["claude_credentials"]["source"] == "inherit"
