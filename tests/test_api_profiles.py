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
