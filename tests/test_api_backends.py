"""Tests for GET /api/v1/backends — the harness capability catalog."""


async def test_list_backends_shape(client):
    r = await client.get("/api/v1/backends")
    assert r.status_code == 200, r.text
    body = r.json()
    codes = {b["code"] for b in body}
    assert {"claude_sdk", "opencode"} <= codes

    for b in body:
        assert set(b.keys()) >= {
            "code", "label", "summary", "protocol_kinds", "multi_endpoint",
            "requires_provider", "enabled", "executable", "group_keys",
            "model_slots", "capabilities",
        }
        # protocol_kinds and capabilities are sorted lists.
        assert b["protocol_kinds"] == sorted(b["protocol_kinds"])
        assert b["capabilities"] == sorted(b["capabilities"])
        for slot in b["model_slots"]:
            assert set(slot.keys()) >= {"name", "label", "required"}


async def test_claude_sdk_backend_shape(client):
    r = await client.get("/api/v1/backends")
    body = {b["code"]: b for b in r.json()}
    cc = body["claude_sdk"]
    assert cc["multi_endpoint"] is False
    assert len(cc["model_slots"]) == 6
    assert {"anthropic", "anthropic_compat"} <= set(cc["protocol_kinds"])
    assert "provider" in cc["group_keys"]
    assert "claude_auth" in cc["group_keys"]


async def test_opencode_backend_shape(client):
    r = await client.get("/api/v1/backends")
    body = {b["code"]: b for b in r.json()}
    oc = body["opencode"]
    assert oc["multi_endpoint"] is True
    assert oc["requires_provider"] is True
    assert "provider" in oc["group_keys"]


async def test_backends_requires_auth(app):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as unauth_client:
        r = await unauth_client.get("/api/v1/backends")
        assert r.status_code in (401, 403)
