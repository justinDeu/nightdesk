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

    # Create wakes by default: the inbox carries the queued no-op wake turn
    # that makes the supervisor spawn a host.
    r = await client.get(f"/api/v1/agents/{a['id']}")
    assert r.status_code == 200
    assert [(t["kind"], t["status"]) for t in r.json()["turns"]] == [("wake", "queued")]

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


async def test_create_is_host_eligible(client, session):
    """A freshly created agent must not sit cold: it shows up in the
    supervisor's spawn list on the next tick, exactly like a woken one."""
    from nightdesk.worker.session_reaper import sessions_needing_host

    a = await _create(client)
    assert a["id"] in [r.id for r in sessions_needing_host(session)]


async def test_create_wake_false_stays_parked(client, session):
    from nightdesk.worker.session_reaper import sessions_needing_host

    a = await _create(client, wake=False)
    r = await client.get(f"/api/v1/agents/{a['id']}")
    assert r.json()["turns"] == [] and r.json()["liveness"] == "cold"
    assert a["id"] not in [r.id for r in sessions_needing_host(session)]


async def test_wake_endpoint_makes_agent_host_eligible(client, session):
    """The explicit wake endpoint enqueues the same no-op wake turn create
    uses, and does not stack a second one on repeat wakes."""
    from nightdesk.worker.session_reaper import sessions_needing_host

    a = await _create(client, wake=False)
    assert a["id"] not in [r.id for r in sessions_needing_host(session)]

    assert (await client.post(f"/api/v1/agents/{a['id']}/wake")).status_code == 202
    assert a["id"] in [r.id for r in sessions_needing_host(session)]

    assert (await client.post(f"/api/v1/agents/{a['id']}/wake")).status_code == 202
    turns = (await client.get(f"/api/v1/agents/{a['id']}")).json()["turns"]
    assert [t["kind"] for t in turns] == ["wake"]


async def test_wake_turn_does_not_consume_message_cap(client, session):
    """The queued wake turn is a control marker: with max_queued_turns=1 the
    first user message must still fit; the second hits the cap."""
    from nightdesk.db.models import ConfigRow
    session.add(ConfigRow(id=1, worktree_root="/w", transcript_root="/t",
                          max_queued_turns=1))
    session.commit()
    a = await _create(client)  # wake turn queued
    assert (await client.post(f"/api/v1/agents/{a['id']}/messages",
                              json={"message": "1"})).status_code == 202
    assert (await client.post(f"/api/v1/agents/{a['id']}/messages",
                              json={"message": "2"})).status_code == 429


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


async def test_turn_queue_list_edit_reorder_cancel(client, session):
    from nightdesk.db.models import SessionTurn
    # wake=False keeps the create-time wake turn out of the queue under test.
    a = await _create(client, wake=False)
    ids = []
    for msg in ("one", "two", "three"):
        r = await client.post(f"/api/v1/agents/{a['id']}/messages", json={"message": msg})
        ids.append(r.json()["id"])

    # GET queue: all three queued, in order.
    r = await client.get(f"/api/v1/agents/{a['id']}/turns")
    assert r.status_code == 200
    assert [t["body"] for t in r.json()] == ["one", "two", "three"]

    # PATCH body (queued-only).
    r = await client.patch(f"/api/v1/agents/{a['id']}/turns/{ids[0]}",
                           json={"body": "edited"})
    assert r.status_code == 200 and r.json()["body"] == "edited"

    # Reorder: reverse.
    r = await client.post(f"/api/v1/agents/{a['id']}/turns/reorder",
                          json={"ordered_ids": [ids[2], ids[1], ids[0]]})
    assert r.status_code == 200
    assert [t["id"] for t in r.json()] == [ids[2], ids[1], ids[0]]

    # DELETE cancels a queued turn.
    r = await client.delete(f"/api/v1/agents/{a['id']}/turns/{ids[1]}")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    r = await client.get(f"/api/v1/agents/{a['id']}/turns")
    assert ids[1] not in [t["id"] for t in r.json()]


async def test_turn_edit_and_cancel_409_when_not_queued(client, session):
    from nightdesk.db.models import SessionTurn
    a = await _create(client)
    turn = SessionTurn(session_id=a["id"], position=1, kind="user",
                       body="x", status="streaming")
    session.add(turn)
    session.commit()
    r = await client.patch(f"/api/v1/agents/{a['id']}/turns/{turn.id}",
                           json={"body": "nope"})
    assert r.status_code == 409
    r = await client.delete(f"/api/v1/agents/{a['id']}/turns/{turn.id}")
    assert r.status_code == 409


async def test_reap_409_when_streaming_or_pending(client, session):
    from nightdesk.db.models import SessionTurn
    from nightdesk.domain import sessions as sess
    a = await _create(client)
    # Streaming -> 409.
    turn = SessionTurn(session_id=a["id"], position=1, kind="user",
                       body="x", status="streaming")
    session.add(turn)
    session.commit()
    assert (await client.post(f"/api/v1/agents/{a['id']}/reap")).status_code == 409
    turn.status = "done"
    session.commit()
    # Open pending -> 409.
    sess.create_pending(session, a["id"], request_id="r1", kind="permission",
                        tool="Bash", payload={})
    session.commit()
    assert (await client.post(f"/api/v1/agents/{a['id']}/reap")).status_code == 409


async def test_reap_noop_when_cold(client):
    a = await _create(client)
    # Cold agent (no live host) -> reap is a no-op, 202.
    r = await client.post(f"/api/v1/agents/{a['id']}/reap")
    assert r.status_code == 202


async def test_reap_enqueues_control_turn_when_live(client, session):
    import os
    from nightdesk.db.models import Session as SessionModel, SessionTurn
    a = await _create(client)
    row = session.get(SessionModel, a["id"])
    row.host_pid = os.getpid()  # pretend a live host owns it
    row.status = "active"
    session.commit()
    r = await client.post(f"/api/v1/agents/{a['id']}/reap")
    assert r.status_code == 202
    reap_turns = session.query(SessionTurn).filter_by(
        session_id=a["id"], kind="reap").all()
    assert len(reap_turns) == 1 and reap_turns[0].status == "queued"


async def test_detail_exposes_claude_session_id(client, session):
    from nightdesk.db.models import Session as SessionModel
    a = await _create(client)
    # Null until the agent has run.
    r = await client.get(f"/api/v1/agents/{a['id']}")
    assert r.json()["claude_session_id"] is None
    row = session.get(SessionModel, a["id"])
    row.resume_handle = {"session_id": "cc-abc-123"}
    session.commit()
    r = await client.get(f"/api/v1/agents/{a['id']}")
    assert r.json()["claude_session_id"] == "cc-abc-123"


async def test_config_exposes_session_knobs(client):
    r = await client.get("/api/v1/config")
    assert r.status_code == 200
    body = r.json()
    assert body["session_idle_timeout_s"] == 300 and body["max_live_sessions"] == 4
    r = await client.patch("/api/v1/config", json={"session_idle_timeout_s": 120})
    assert r.status_code == 200 and r.json()["session_idle_timeout_s"] == 120


# ---------------------------------------------------------------------------
# Attention: unread replies + seen / mark-unread
# ---------------------------------------------------------------------------
def _complete_user_turn(session, agent_id, turn_id=None):
    """Force a user turn into 'done' with a fresh finished_at (what the host
    does when the assistant's reply lands)."""
    from datetime import datetime, timezone
    from nightdesk.db.models import SessionTurn
    if turn_id is None:
        turn = SessionTurn(session_id=agent_id, position=99, kind="user",
                           body="hi", status="done",
                           finished_at=datetime.now(timezone.utc))
        session.add(turn)
    else:
        turn = session.get(SessionTurn, turn_id)
        turn.status = "done"
        turn.finished_at = datetime.now(timezone.utc)
    session.commit()
    return turn


async def test_unread_appears_after_completed_turn_and_clears_on_seen(client, session):
    a = await _create(client)

    # Fresh agent: no completed user turn -> never unread, even with a NULL
    # seen stamp.
    r = await client.get("/api/v1/agents/attention")
    assert r.status_code == 200
    assert r.json()["unread"] == [] and r.json()["pending"] == []
    assert r.json()["total"] == 0

    # Post a message and complete it (the host's reply landed) with the seen
    # stamp still NULL -> unread.
    r = await client.post(f"/api/v1/agents/{a['id']}/messages", json={"message": "hi"})
    _complete_user_turn(session, a["id"], r.json()["id"])

    r = await client.get("/api/v1/agents/attention")
    body = r.json()
    assert [u["session_id"] for u in body["unread"]] == [a["id"]]
    assert body["unread"][0]["session_title"] == "A"
    assert body["total"] == 1

    # The list rows carry the unread flag (agents-page dot).
    r = await client.get("/api/v1/agents")
    assert next(x for x in r.json() if x["id"] == a["id"])["unread"] is True

    # Seen stamp clears it.
    r = await client.post(f"/api/v1/agents/{a['id']}/seen")
    assert r.status_code == 200 and r.json()["unread"] is False
    r = await client.get("/api/v1/agents/attention")
    assert r.json()["unread"] == [] and r.json()["total"] == 0

    # A NEW completed reply after the stamp -> unread again.
    _complete_user_turn(session, a["id"])
    r = await client.get("/api/v1/agents/attention")
    assert [u["session_id"] for u in r.json()["unread"]] == [a["id"]]


async def test_mark_unread_rewinds_seen(client, session):
    a = await _create(client)
    r = await client.post(f"/api/v1/agents/{a['id']}/messages", json={"message": "hi"})
    _complete_user_turn(session, a["id"], r.json()["id"])

    # View it (seen) -> read.
    await client.post(f"/api/v1/agents/{a['id']}/seen")
    r = await client.get("/api/v1/agents/attention")
    assert r.json()["unread"] == []

    # Mark-unread re-raises attention despite the recent view.
    r = await client.delete(f"/api/v1/agents/{a['id']}/seen")
    assert r.status_code == 200 and r.json()["unread"] is True
    r = await client.get("/api/v1/agents/attention")
    assert [u["session_id"] for u in r.json()["unread"]] == [a["id"]]
    assert r.json()["total"] == 1


async def test_mark_unread_on_fresh_agent_is_inert(client):
    a = await _create(client)
    # No completed user turn: clearing the stamp cannot invent attention.
    r = await client.delete(f"/api/v1/agents/{a['id']}/seen")
    assert r.status_code == 200 and r.json()["unread"] is False
    r = await client.get("/api/v1/agents/attention")
    assert r.json()["unread"] == [] and r.json()["total"] == 0


async def test_attention_combines_pending_and_unread(client, session):
    from nightdesk.domain import sessions as sess

    blocked = await _create(client, title="Blocked")
    sess.create_pending(session, blocked["id"], request_id="r1",
                        kind="permission", tool="Bash", payload={})
    replied = await _create(client, title="Replied")
    _complete_user_turn(session, replied["id"])

    r = await client.get("/api/v1/agents/attention")
    body = r.json()
    assert [p["session_id"] for p in body["pending"]] == [blocked["id"]]
    assert body["pending"][0]["session_title"] == "Blocked"
    assert [u["session_id"] for u in body["unread"]] == [replied["id"]]
    assert body["total"] == 2

    # /agents/pending keeps working for existing callers (pending only).
    r = await client.get("/api/v1/agents/pending")
    assert [p["session_id"] for p in r.json()] == [blocked["id"]]


async def test_ended_agent_never_unread(client, session):
    a = await _create(client)
    _complete_user_turn(session, a["id"])
    r = await client.post(f"/api/v1/agents/{a['id']}/end")
    assert r.status_code == 200
    session.expire_all()
    r = await client.get("/api/v1/agents/attention")
    assert r.json()["unread"] == []
