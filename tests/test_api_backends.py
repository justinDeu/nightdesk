"""Tests for GET /api/v1/backends — the harness capability catalog."""
import stat


def _make_shim(tmp_path, name, output="9.9.9"):
    shim = tmp_path / name
    shim.write_text(f"#!/bin/sh\necho '{output}'\nexit 0\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


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
            "model_slots", "capabilities", "runtime",
        }
        # protocol_kinds and capabilities are sorted lists.
        assert b["protocol_kinds"] == sorted(b["protocol_kinds"])
        assert b["capabilities"] == sorted(b["capabilities"])
        for slot in b["model_slots"]:
            assert set(slot.keys()) >= {"name", "label", "required"}


async def test_claude_and_opencode_have_runtime_status(client):
    r = await client.get("/api/v1/backends")
    body = {b["code"]: b for b in r.json()}
    for code in ("claude_sdk", "opencode"):
        runtime = body[code]["runtime"]
        assert runtime is not None
        assert set(runtime.keys()) == {
            "binary_path_override", "resolved_path", "source", "found", "version",
        }
        assert runtime["source"] in ("override", "path", "default")


async def test_runtime_reflects_config_row_override(client, session, tmp_path):
    from nightdesk.db.models import ConfigRow

    shim = _make_shim(tmp_path, "claude", "7.7.7")
    row = session.get(ConfigRow, 1)
    if row is None:
        row = ConfigRow(id=1, worktree_root=str(tmp_path), transcript_root=str(tmp_path))
        session.add(row)
    row.claude_binary_path = str(shim)
    session.commit()

    r = await client.get("/api/v1/backends")
    cc = {b["code"]: b for b in r.json()}["claude_sdk"]["runtime"]
    assert cc["source"] == "override"
    assert cc["binary_path_override"] == str(shim)
    assert cc["resolved_path"] == str(shim)
    assert cc["found"] is True
    assert cc["version"] == "7.7.7"


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
