"""JSON API for interactive sessions (``/api/v1/sessions/*``)."""
from __future__ import annotations

import pytest

from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket, get_ticket


def _profile(session):
    return create_profile(
        session, name="p", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )


async def test_create_and_get_session(client, session):
    p = _profile(session)
    r = await client.post("/api/v1/sessions",
                          json={"title": "chat", "profile_id": p.id})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "session"
    assert body["status"] == "draft"
    assert body["conversations"] == []
    sid = body["id"]

    got = await client.get(f"/api/v1/sessions/{sid}")
    assert got.status_code == 200
    assert got.json()["id"] == sid


async def test_list_sessions_excludes_normal_tickets(client, session):
    p = _profile(session)
    create_ticket(session, title="board", prompt="p", priority=0,
                  profile_id=p.id, status="draft", source_path="/tmp")
    r = await client.post("/api/v1/sessions", json={"profile_id": p.id})
    sid = r.json()["id"]

    lst = await client.get("/api/v1/sessions")
    assert lst.status_code == 200
    ids = {s["id"] for s in lst.json()}
    assert ids == {sid}


async def test_session_never_appears_on_board(client, session):
    p = _profile(session)
    r = await client.post("/api/v1/sessions", json={"profile_id": p.id})
    sid = r.json()["id"]
    board = await client.get("/api/v1/tickets")
    assert sid not in {t["id"] for t in board.json()}


async def test_post_message_queues_turn(client, session):
    p = _profile(session)
    r = await client.post("/api/v1/sessions", json={"profile_id": p.id})
    sid = r.json()["id"]
    msg = await client.post(f"/api/v1/sessions/{sid}/messages",
                            json={"message": "hello"})
    assert msg.status_code == 200, msg.text
    body = msg.json()
    assert body["status"] == "queued"
    assert body["run_now"] is True


async def test_post_message_busy_is_409(client, session):
    p = _profile(session)
    r = await client.post("/api/v1/sessions", json={"profile_id": p.id})
    sid = r.json()["id"]
    t = get_ticket(session, sid)
    t.status = "running"
    session.commit()
    msg = await client.post(f"/api/v1/sessions/{sid}/messages",
                            json={"message": "hi"})
    assert msg.status_code == 409


@pytest.mark.parametrize("bad", ["", "   "])
async def test_post_empty_message_is_422(client, session, bad):
    p = _profile(session)
    r = await client.post("/api/v1/sessions", json={"profile_id": p.id})
    sid = r.json()["id"]
    msg = await client.post(f"/api/v1/sessions/{sid}/messages",
                            json={"message": bad})
    assert msg.status_code == 422


async def test_promote_session(client, session):
    p = _profile(session)
    r = await client.post("/api/v1/sessions", json={"profile_id": p.id})
    sid = r.json()["id"]
    promo = await client.post(f"/api/v1/sessions/{sid}/promote",
                              json={"title": "Real", "target_status": "draft"})
    assert promo.status_code == 200, promo.text
    body = promo.json()
    assert body["kind"] == "ticket"
    assert body["title"] == "Real"
    # Now visible on the board.
    board = await client.get("/api/v1/tickets")
    assert sid in {t["id"] for t in board.json()}


async def test_archive_and_delete_session(client, session):
    p = _profile(session)
    r = await client.post("/api/v1/sessions", json={"profile_id": p.id})
    sid = r.json()["id"]
    arch = await client.post(f"/api/v1/sessions/{sid}/archive")
    assert arch.status_code == 200
    assert arch.json()["status"] == "archived"

    dele = await client.delete(f"/api/v1/sessions/{sid}")
    assert dele.status_code == 204
    assert (await client.get(f"/api/v1/sessions/{sid}")).status_code == 404


async def test_session_endpoints_404_for_missing_or_normal(client, session):
    p = _profile(session)
    # missing
    assert (await client.get("/api/v1/sessions/nope")).status_code == 404
    # a normal ticket is not addressable as a session
    t = create_ticket(session, title="n", prompt="p", priority=0,
                      profile_id=p.id, status="draft", source_path="/tmp")
    assert (await client.get(f"/api/v1/sessions/{t.id}")).status_code == 404
    assert (await client.post(f"/api/v1/sessions/{t.id}/messages",
                              json={"message": "x"})).status_code == 404
