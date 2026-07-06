"""Tests for /api/v1/providers and /api/v1/provider-endpoints."""
import httpx

from nightdesk.db.models import Profile, ProviderEndpoint


def _zai_payload(**overrides):
    payload = {
        "name": "ZAI",
        "vendor": "zai",
        "credential_value": "sekret",
        "endpoints": [
            {
                "label": "Anthropic-compatible",
                "protocol_kind": "anthropic_compat",
                "base_url": "https://api.z.ai/api/anthropic",
            },
            {
                "label": "OpenAI-compatible",
                "protocol_kind": "openai_compat",
                "base_url": "https://api.z.ai/api/paas/v4",
            },
        ],
    }
    payload.update(overrides)
    return payload


async def test_create_provider_nests_endpoints_and_seeds_credential(client):
    r = await client.post("/api/v1/providers", json=_zai_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "ZAI"
    assert body["vendor"] == "zai"
    assert len(body["endpoints"]) == 2
    for ep in body["endpoints"]:
        # Seeded credential lands on every nested endpoint whose own
        # credential_value was unset and whose source is api_key.
        assert ep["credential_set"] is True
        # Never echoed back.
        assert "credential" not in ep
        assert "credential_value" not in ep


async def test_seeded_credential_does_not_override_explicit_value(client):
    payload = _zai_payload()
    payload["endpoints"][0]["credential_value"] = "explicit-key"
    r = await client.post("/api/v1/providers", json=payload)
    assert r.status_code == 201, r.text
    # Both should still be marked as set; we can't observe plaintext here,
    # but the seeded one and the explicit one must both persist.
    for ep in r.json()["endpoints"]:
        assert ep["credential_set"] is True


async def test_extra_is_write_only(client):
    payload = _zai_payload()
    payload["endpoints"][0]["extra"] = {"routing_token": "abc"}
    r = await client.post("/api/v1/providers", json=payload)
    assert r.status_code == 201, r.text
    ep0 = r.json()["endpoints"][0]
    assert ep0["extra_set"] is True
    assert "extra" not in ep0


async def test_provider_crud(client):
    r = await client.post("/api/v1/providers", json=_zai_payload())
    pid = r.json()["id"]

    r = await client.get("/api/v1/providers")
    assert r.status_code == 200
    assert any(p["id"] == pid for p in r.json())

    r = await client.get(f"/api/v1/providers/{pid}")
    assert r.status_code == 200

    r = await client.patch(f"/api/v1/providers/{pid}", json={"name": "ZAI Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "ZAI Renamed"

    r = await client.delete(f"/api/v1/providers/{pid}")
    assert r.status_code == 204

    r = await client.get(f"/api/v1/providers/{pid}")
    assert r.status_code == 404


async def test_provider_name_conflict(client):
    r = await client.post("/api/v1/providers", json=_zai_payload())
    assert r.status_code == 201
    r = await client.post("/api/v1/providers", json=_zai_payload())
    assert r.status_code == 409


async def test_endpoint_crud(client):
    r = await client.post("/api/v1/providers", json={"name": "Bare", "vendor": "custom"})
    pid = r.json()["id"]

    r = await client.post(
        f"/api/v1/providers/{pid}/endpoints",
        json={"label": "Key", "protocol_kind": "openai", "credential_value": "k"},
    )
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    assert r.json()["credential_set"] is True

    r = await client.patch(
        f"/api/v1/provider-endpoints/{eid}", json={"default_model": "gpt-5.4"},
    )
    assert r.status_code == 200
    assert r.json()["default_model"] == "gpt-5.4"

    r = await client.delete(f"/api/v1/provider-endpoints/{eid}")
    assert r.status_code == 204


async def test_endpoint_update_credential_value_absent_keeps_existing(client):
    r = await client.post("/api/v1/providers", json=_zai_payload())
    eid = r.json()["endpoints"][0]["id"]

    r = await client.patch(f"/api/v1/provider-endpoints/{eid}", json={"label": "renamed"})
    assert r.status_code == 200
    assert r.json()["label"] == "renamed"
    assert r.json()["credential_set"] is True


async def test_catalog_route_shape(client):
    r = await client.get("/api/v1/providers/catalog")
    assert r.status_code == 200
    body = r.json()
    vendors = {v["vendor"] for v in body}
    assert {"zai", "anthropic", "openai", "openrouter", "ollama"} <= vendors
    zai = next(v for v in body if v["vendor"] == "zai")
    assert len(zai["endpoints"]) == 2
    assert zai["endpoints"][0]["protocol_kind"] == "anthropic_compat"


async def test_delete_provider_blocked_by_direct_profile_reference(client, session):
    r = await client.post("/api/v1/providers", json=_zai_payload())
    pid = r.json()["id"]
    eid = r.json()["endpoints"][0]["id"]

    session.add(Profile(
        name="uses-zai", backend="claude_sdk", endpoint_id=eid,
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    ))
    session.commit()

    r = await client.delete(f"/api/v1/provider-endpoints/{eid}")
    assert r.status_code == 409

    r = await client.delete(f"/api/v1/providers/{pid}")
    assert r.status_code == 409


async def test_delete_endpoint_blocked_by_agent_backend_config_reference(client, session):
    r = await client.post("/api/v1/providers", json=_zai_payload())
    eid = r.json()["endpoints"][1]["id"]

    session.add(Profile(
        name="opencode-mixed", backend="opencode",
        backend_config={"agents": [{"name": "researcher", "endpoint_id": eid}]},
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    ))
    session.commit()

    r = await client.delete(f"/api/v1/provider-endpoints/{eid}")
    assert r.status_code == 409


async def test_rotate_credential_only_touches_api_key_endpoints(client, session):
    payload = _zai_payload()
    payload["endpoints"].append({
        "label": "Local", "protocol_kind": "ollama", "credential_source": "none",
    })
    r = await client.post("/api/v1/providers", json=payload)
    pid = r.json()["id"]
    ollama_eid = r.json()["endpoints"][2]["id"]

    r = await client.post(f"/api/v1/providers/{pid}/rotate-credential", json={"credential_value": "new-key"})
    assert r.status_code == 200
    assert r.json()["rotated"] == 2

    ollama_row = session.get(ProviderEndpoint, ollama_eid)
    assert ollama_row.credential is None


# --- pull-models -----------------------------------------------------------


_RealClient = httpx.Client


def _patch_client(monkeypatch, handler) -> None:
    """Route every ``httpx.Client(...)`` construction through a mock transport
    that calls ``handler``, leaving everything else (timeout, headers) as the
    caller passed it."""
    def _fake_client(**kwargs):
        kwargs.pop("transport", None)
        return _RealClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", _fake_client)


async def test_pull_models_openai_compat_success(client, monkeypatch):
    r = await client.post("/api/v1/providers", json={
        "name": "OpenAICompatVendor", "vendor": "custom",
        "endpoints": [{
            "label": "compat", "protocol_kind": "openai_compat",
            "base_url": "https://example.test/v1", "credential_value": "k",
        }],
    })
    eid = r.json()["endpoints"][0]["id"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://example.test/v1/models"
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"data": [{"id": "glm-5.2"}, {"id": "glm-4.5"}]})

    _patch_client(monkeypatch, handler)

    r = await client.post(f"/api/v1/provider-endpoints/{eid}/pull-models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["models"] == ["glm-5.2", "glm-4.5"]
    assert body["models_pulled_at"] is not None


async def test_pull_models_ollama_success(client, monkeypatch):
    r = await client.post("/api/v1/providers", json={
        "name": "LocalOllama", "vendor": "ollama",
        "endpoints": [{
            "label": "local", "protocol_kind": "ollama", "credential_source": "none",
            "base_url": "http://localhost:11434",
        }],
    })
    eid = r.json()["endpoints"][0]["id"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://localhost:11434/api/tags"
        return httpx.Response(200, json={"models": [{"name": "llama3"}, {"name": "qwen"}]})

    _patch_client(monkeypatch, handler)

    r = await client.post(f"/api/v1/provider-endpoints/{eid}/pull-models")
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["llama3", "qwen"]


async def test_pull_models_codex_returns_400(client):
    r = await client.post("/api/v1/providers", json={
        "name": "OpenAICodex", "vendor": "openai",
        "endpoints": [{
            "label": "codex", "protocol_kind": "openai_codex",
            "credential_source": "oauth_file", "credential_value": "~/.codex/auth.json",
        }],
    })
    eid = r.json()["endpoints"][0]["id"]

    r = await client.post(f"/api/v1/provider-endpoints/{eid}/pull-models")
    assert r.status_code == 400
    assert "curate manually" in r.json()["detail"]


async def test_pull_models_failure_returns_502(client, monkeypatch):
    r = await client.post("/api/v1/providers", json={
        "name": "Flaky", "vendor": "custom",
        "endpoints": [{
            "label": "compat", "protocol_kind": "openai_compat",
            "base_url": "https://example.test/v1", "credential_value": "k",
        }],
    })
    eid = r.json()["endpoints"][0]["id"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _patch_client(monkeypatch, handler)

    r = await client.post(f"/api/v1/provider-endpoints/{eid}/pull-models")
    assert r.status_code == 502
