"""Resident agents JSON API (``/api/v1/agents``) + fs include_files + config."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def _create(client, **over):
    payload = {"profile_id": "p", "title": "A"}
    payload.update(over)
    r = await client.post("/api/v1/agents", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_list_get_delete(client):
    a = await _create(client)
    assert a["status"] == "idle" and a["liveness"] == "cold"
    assert a["backend"] == "claude"

    r = await client.get("/api/v1/agents")
    assert r.status_code == 200 and any(x["id"] == a["id"] for x in r.json())

    r = await client.get(f"/api/v1/agents/{a['id']}")
    assert r.status_code == 200 and r.json()["turns"] == []

    r = await client.delete(f"/api/v1/agents/{a['id']}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/agents/{a['id']}")
    assert r.status_code == 404


async def test_message_enqueues_turn(client):
    a = await _create(client)
    r = await client.post(f"/api/v1/agents/{a['id']}/messages", json={"message": "hi"})
    assert r.status_code == 202
    body = r.json()
    assert body["kind"] == "user" and body["status"] == "queued"


async def test_message_empty_rejected(client):
    a = await _create(client)
    r = await client.post(f"/api/v1/agents/{a['id']}/messages", json={"message": "   "})
    assert r.status_code == 422


async def test_queue_cap_returns_429(client, session):
    from nightdesk.db.models import ConfigRow
    session.add(ConfigRow(id=1, worktree_root="/w", transcript_root="/t",
                          max_queued_turns=1))
    session.commit()
    a = await _create(client)
    assert (await client.post(f"/api/v1/agents/{a['id']}/messages",
                              json={"message": "1"})).status_code == 202
    r = await client.post(f"/api/v1/agents/{a['id']}/messages", json={"message": "2"})
    assert r.status_code == 429


async def test_interrupt_409_when_nothing_in_flight(client):
    a = await _create(client)
    r = await client.post(f"/api/v1/agents/{a['id']}/interrupt")
    assert r.status_code == 409


async def test_end_is_terminal(client):
    a = await _create(client)
    r = await client.post(f"/api/v1/agents/{a['id']}/end")
    assert r.status_code == 200 and r.json()["status"] == "ended"
    assert r.json()["liveness"] == "ended"


async def test_pending_aggregation_and_answer_409(client, session):
    from nightdesk.domain import sessions as sess
    a = await _create(client)
    sess.create_pending(session, a["id"], request_id="r1", kind="permission",
                        tool="Bash", payload={"x": 1})
    session.commit()

    r = await client.get("/api/v1/agents/pending")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1 and items[0]["request_id"] == "r1"
    assert items[0]["session_title"] == "A"

    # Detail surfaces the pending input.
    r = await client.get(f"/api/v1/agents/{a['id']}")
    assert r.json()["pending_input"]["request_id"] == "r1"
    assert r.json()["has_pending"] is True and r.json()["liveness"] == "needs-input"

    # Answer once -> 202; the durable answer turn is enqueued.
    r = await client.post(f"/api/v1/agents/{a['id']}/pending/r1",
                          json={"decision": "allow"})
    assert r.status_code == 202 and r.json()["kind"] == "answer"

    # Resolve it, then a second answer 409s.
    sess.resolve_pending(session, a["id"], "r1", status="answered")
    session.commit()
    r = await client.post(f"/api/v1/agents/{a['id']}/pending/r1",
                          json={"decision": "allow"})
    assert r.status_code == 409


async def test_env_put_masks_and_preserves(client):
    a = await _create(client)
    r = await client.put(f"/api/v1/agents/{a['id']}/env", json={"env": {
        "PLAIN": {"value": "p", "secret": False},
        "SECRET": {"value": "s", "secret": True},
    }})
    assert r.status_code == 200
    env = {e["key"]: e for e in r.json()["env"]}
    assert env["PLAIN"]["value"] == "p"
    assert env["SECRET"]["secret"] is True and env["SECRET"].get("value") is None

    # Re-PUT the secret with value null keeps it set (write-only).
    r = await client.put(f"/api/v1/agents/{a['id']}/env", json={"env": {
        "SECRET": {"value": None, "secret": True},
    }})
    env = {e["key"]: e for e in r.json()["env"]}
    assert env["SECRET"]["set"] is True
    assert "PLAIN" not in env  # replace semantics


async def test_restart_409_when_streaming(client, session):
    from nightdesk.db.models import SessionTurn
    a = await _create(client)
    session.add(SessionTurn(session_id=a["id"], position=1, kind="user",
                            body="x", status="streaming"))
    session.commit()
    r = await client.post(f"/api/v1/agents/{a['id']}/restart-runtime", json={})
    assert r.status_code == 409
    r = await client.post(f"/api/v1/agents/{a['id']}/restart-runtime",
                          json={"force": True})
    assert r.status_code == 202


async def test_wake_202(client):
    a = await _create(client)
    r = await client.post(f"/api/v1/agents/{a['id']}/wake")
    assert r.status_code == 202


async def test_fs_suggest_include_files(client, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "file.txt").write_text("x")
    # Directory-only (default): the file is not suggested.
    r = await client.get("/api/v1/fs/suggest", params={"prefix": str(tmp_path) + "/"})
    assert not any(m.endswith("file.txt") for m in r.json()["matches"])
    # include_files: the file appears (no trailing slash).
    r = await client.get("/api/v1/fs/suggest",
                         params={"prefix": str(tmp_path) + "/", "include_files": True})
    matches = r.json()["matches"]
    assert any(m.endswith("file.txt") for m in matches)
    assert any(m.endswith("sub/") for m in matches)


async def test_config_exposes_session_knobs(client):
    r = await client.get("/api/v1/config")
    assert r.status_code == 200
    body = r.json()
    assert body["session_idle_timeout_s"] == 300 and body["max_live_sessions"] == 4
    r = await client.patch("/api/v1/config", json={"session_idle_timeout_s": 120})
    assert r.status_code == 200 and r.json()["session_idle_timeout_s"] == 120
