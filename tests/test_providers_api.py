"""Tests for /api/v1/providers and /api/v1/provider-endpoints."""
import json

import httpx

from nightdesk.api.routes import providers as providers_route
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
    keys = {o["key"] for o in body}
    assert {
        "openai_api", "openai_codex", "anthropic_api", "claude_subscription",
        "zai", "zai_coding", "openrouter", "ollama",
    } <= keys
    for offering in body:
        assert offering["credential_source"], offering["key"]
        assert "credential_source" not in offering["endpoints"][0]
    zai = next(o for o in body if o["key"] == "zai")
    assert zai["credential_source"] == "api_key"
    assert len(zai["endpoints"]) == 2
    assert zai["endpoints"][0]["protocol_kind"] == "anthropic_compat"


async def test_catalog_codex_seeds_default_models(client):
    r = await client.get("/api/v1/providers/catalog")
    codex = next(o for o in r.json() if o["key"] == "openai_codex")
    ep = codex["endpoints"][0]
    assert ep["protocol_kind"] == "openai_codex"
    assert ep["models"], "codex should seed default model ids, not an empty list"
    assert ep["default_model"] in ep["models"]


async def test_protocols_route_marks_codex_as_supporting_model_list(client):
    r = await client.get("/api/v1/providers/protocols")
    assert r.status_code == 200
    by_key = {p["key"]: p["supports_model_list"] for p in r.json()}
    assert by_key["openai_codex"] is True
    assert by_key["openai"] is True
    assert by_key["anthropic_compat"] is True


async def test_create_provider_rejects_mixed_secret_and_file_credentials(client):
    r = await client.post("/api/v1/providers", json={
        "name": "Mixed",
        "vendor": "openai",
        "endpoints": [
            {"protocol_kind": "openai", "credential_source": "api_key", "credential_value": "sk-abc"},
            {"protocol_kind": "openai_codex", "credential_source": "oauth_file", "credential_value": "~/.codex/auth.json"},
        ],
    })
    assert r.status_code == 400
    assert "credential mode" in r.json()["detail"]


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


async def _create_codex_endpoint(client, *, auth_path) -> str:
    r = await client.post("/api/v1/providers", json={
        "name": "OpenAICodex", "vendor": "openai",
        "endpoints": [{
            "label": "codex", "protocol_kind": "openai_codex",
            "credential_source": "oauth_file", "credential_value": str(auth_path),
        }],
    })
    return r.json()["endpoints"][0]["id"]


async def test_pull_models_codex_success_filters_hidden_visibility(client, monkeypatch, tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({
        "tokens": {
            "access_token": "codex-access-token",
            "refresh_token": "codex-refresh-token",
            "account_id": "acct-123",
        },
    }))
    eid = await _create_codex_endpoint(client, auth_path=auth_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://chatgpt.com/backend-api/codex/models"
        assert request.headers["authorization"] == "Bearer codex-access-token"
        assert request.headers["chatgpt-account-id"] == "acct-123"
        return httpx.Response(200, json={"models": [
            {"slug": "gpt-5.6-sol", "visibility": "list"},
            {"slug": "gpt-5.5", "visibility": "list"},
            # Hidden internal models must not enter the menu.
            {"slug": "codex-auto-review", "visibility": "hide"},
        ]})

    _patch_client(monkeypatch, handler)

    r = await client.post(f"/api/v1/provider-endpoints/{eid}/pull-models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["models"] == ["gpt-5.6-sol", "gpt-5.5"]
    assert body["models_pulled_at"] is not None


async def test_pull_models_codex_falls_back_to_local_cache_on_fetch_failure(
    client, monkeypatch, tmp_path,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"tokens": {"access_token": "codex-access-token"}}))
    eid = await _create_codex_endpoint(client, auth_path=auth_path)

    cache_path = tmp_path / "models_cache.json"
    cache_path.write_text(json.dumps({
        "fetched_at": "2026-07-16T00:00:00Z",
        "etag": "some-etag",
        "client_version": "0.144.5",
        "models": [
            {"slug": "gpt-5.6-sol", "visibility": "list"},
            {"slug": "codex-auto-review", "visibility": "hide"},
        ],
    }))
    monkeypatch.setattr(providers_route, "_CODEX_MODELS_CACHE_PATH", str(cache_path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream unavailable")

    _patch_client(monkeypatch, handler)

    r = await client.post(f"/api/v1/provider-endpoints/{eid}/pull-models")
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["gpt-5.6-sol"]


async def test_pull_models_codex_fetch_failure_without_cache_returns_502(
    client, monkeypatch, tmp_path,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"tokens": {"access_token": "codex-access-token"}}))
    eid = await _create_codex_endpoint(client, auth_path=auth_path)

    # Point at a cache file that doesn't exist.
    monkeypatch.setattr(
        providers_route, "_CODEX_MODELS_CACHE_PATH", str(tmp_path / "no_such_cache.json"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream unavailable")

    _patch_client(monkeypatch, handler)

    r = await client.post(f"/api/v1/provider-endpoints/{eid}/pull-models")
    assert r.status_code == 502


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
