"""JSON API tests for the mid-run steering endpoints (/api/v1/tickets/{tid}/steer).

Covers create (409 unless running), list + capability payload, edit/reorder/delete
guards, and the capability-driven delivery-mode downgrade.
"""
from __future__ import annotations

import pytest

from nightdesk.domain.conversations import create_conversation
from nightdesk.domain.tickets import create_ticket, transition_status


def _make_profile(session, *, backend="opencode", **overrides):
    from nightdesk.domain.profiles import create_profile
    fields = dict(
        name="p", fs_read=[], fs_write=["/opt/code"], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None, backend=backend,
    )
    fields.update(overrides)
    return create_profile(session, **fields)


def _running_ticket(session, *, backend="opencode"):
    p = _make_profile(session, backend=backend)
    t = create_ticket(session, title="live", prompt="do it", priority=0,
                      profile_id=p.id, status="queued", source_path="/tmp")
    transition_status(session, t.id, "running")
    conv = create_conversation(
        session, ticket_id=t.id, profile_id=p.id, backend=backend,
        transcript_path="/tmp/c.log",
    )
    t.current_conversation_id = conv.id
    session.commit()
    return t, conv


async def test_add_steer_requires_running(client, session):
    p = _make_profile(session)
    t = create_ticket(session, title="draft", prompt="x", priority=0,
                      profile_id=p.id, status="queued", source_path="/tmp")
    conv = create_conversation(session, ticket_id=t.id, profile_id=p.id,
                               backend="opencode", transcript_path="/tmp/c.log")
    t.current_conversation_id = conv.id
    session.commit()
    r = await client.post(f"/api/v1/tickets/{t.id}/steer", json={"body": "hi"})
    assert r.status_code == 409, r.text


async def test_add_and_list_steer_opencode_reports_inject(client, session):
    t, _conv = _running_ticket(session, backend="opencode")
    r = await client.post(f"/api/v1/tickets/{t.id}/steer",
                          json={"body": "focus on tests", "delivery_mode": "inject"})
    assert r.status_code == 201, r.text
    assert r.json()["delivery_mode"] == "inject"
    assert r.json()["state"] == "pending"

    r = await client.get(f"/api/v1/tickets/{t.id}/steer")
    assert r.status_code == 200
    body = r.json()
    assert body["capability"]["inject"] is True
    assert [m["body"] for m in body["messages"]] == ["focus on tests"]


async def test_inject_downgrades_on_queue_only_backend(client, session):
    t, _conv = _running_ticket(session, backend="claude_sdk")
    r = await client.post(f"/api/v1/tickets/{t.id}/steer",
                          json={"body": "later", "delivery_mode": "inject"})
    assert r.status_code == 201, r.text
    # claude_sdk cannot inject, so the honest stored mode is at_turn.
    assert r.json()["delivery_mode"] == "at_turn"
    r = await client.get(f"/api/v1/tickets/{t.id}/steer")
    assert r.json()["capability"]["inject"] is False


async def test_add_rejects_empty_body(client, session):
    t, _conv = _running_ticket(session)
    r = await client.post(f"/api/v1/tickets/{t.id}/steer", json={"body": "   "})
    assert r.status_code == 422, r.text


async def test_edit_reorder_delete_flow(client, session):
    t, _conv = _running_ticket(session)
    ids = []
    for body in ("a", "b", "c"):
        r = await client.post(f"/api/v1/tickets/{t.id}/steer", json={"body": body})
        ids.append(r.json()["id"])

    # Edit.
    r = await client.patch(f"/api/v1/tickets/{t.id}/steer/{ids[0]}", json={"body": "a2"})
    assert r.status_code == 200 and r.json()["body"] == "a2"

    # Reorder.
    r = await client.post(f"/api/v1/tickets/{t.id}/steer/reorder",
                          json={"ordered_ids": [ids[2], ids[0], ids[1]]})
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["messages"]] == [ids[2], ids[0], ids[1]]

    # Delete (cancel).
    r = await client.delete(f"/api/v1/tickets/{t.id}/steer/{ids[1]}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/tickets/{t.id}/steer")
    remaining = [m["id"] for m in r.json()["messages"]]
    assert ids[1] not in remaining
    assert set(remaining) == {ids[0], ids[2]}


async def test_edit_missing_message_404(client, session):
    t, _conv = _running_ticket(session)
    r = await client.patch(f"/api/v1/tickets/{t.id}/steer/nope", json={"body": "x"})
    assert r.status_code == 404
