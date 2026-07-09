"""The description field: human-facing what/why, split from the agent prompt."""
from __future__ import annotations

import pytest

from nightdesk.db.models import Profile
from nightdesk.domain.events import run_actor
from nightdesk.domain.tickets import create_ticket, get_ticket, transition_status


@pytest.fixture
def profile(session) -> Profile:
    p = Profile(
        name="p", fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    )
    session.add(p)
    session.commit()
    return p


async def _create(client, **kw):
    r = await client.post(
        "/api/v1/profiles",
        json={
            "name": "d", "fs_read": [], "fs_write": [], "allowed_tools": [],
            "denied_tools": [], "network_mode": "off", "network_allowlist": [],
            "secret_keys": [], "claude_credentials": {"source": "inherit"},
        },
    )
    pid = r.json()["id"]
    body = {"title": "t", "profile_id": pid, "source_path": "/tmp", "prompt": "do the thing"}
    body.update(kw)
    r = await client.post("/api/v1/tickets", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_with_description(client):
    t = await _create(client, description="Refactor the auth module")
    assert t["description"] == "Refactor the auth module"
    assert t["prompt"] == "do the thing"  # unchanged, not mirrored


async def test_create_without_description_is_null(client):
    t = await _create(client)
    assert t["description"] is None


async def test_patch_sets_and_clears_description(client):
    t = await _create(client, description="first")
    r = await client.patch(f"/api/v1/tickets/{t['id']}", json={"description": "second"})
    assert r.json()["description"] == "second"
    assert r.json()["prompt"] == "do the thing"
    # Explicit null clears it.
    r = await client.patch(f"/api/v1/tickets/{t['id']}", json={"description": None})
    assert r.json()["description"] is None


async def test_focused_description_patch(client):
    t = await _create(client)
    r = await client.patch(f"/api/v1/tickets/{t['id']}/description", json={"description": "  hi  "})
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "hi"  # trimmed
    # Empty clears.
    r = await client.patch(f"/api/v1/tickets/{t['id']}/description", json={"description": ""})
    assert r.json()["description"] is None


async def test_focused_description_patch_404(client):
    r = await client.patch("/api/v1/tickets/nope/description", json={"description": "x"})
    assert r.status_code == 404


async def test_digest_carries_description(client, session, profile):
    t = create_ticket(
        session, title="t", profile_id=profile.id, source_path="/tmp",
        description="Fix the flaky login test",
    )
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review", actor=run_actor("r1"))
    r = await client.get("/api/v1/tickets/ack/digest")
    dt = r.json()["groups"][0]["tickets"][0]
    assert dt["description"] == "Fix the flaky login test"
