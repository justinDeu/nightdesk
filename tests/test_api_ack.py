"""API + digest tests for the post-review acknowledgement flow (Layers 1-2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from nightdesk.db.models import Profile, TicketEvent
from nightdesk.domain import ack as ack_domain
from nightdesk.domain.events import ADMIN, run_actor
from nightdesk.domain.projects import create_project
from nightdesk.domain.tickets import create_ticket, transition_status


@pytest.fixture
def profile(session) -> Profile:
    p = Profile(
        name="p", fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    )
    session.add(p)
    session.commit()
    return p


def _review_ticket(session, profile, *, project_id=None, actor=None):
    """A ticket sitting in review, moved there by ``actor`` (run by default =>
    unacknowledged)."""
    t = create_ticket(
        session, title="t", profile_id=profile.id, source_path="/tmp",
        project_id=project_id,
    )
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review", actor=actor or run_actor("r1"))
    return t


def _backdate_entering(session, ticket_id, when):
    """Push the ticket's current-status event back in time (for day grouping)."""
    ev = session.scalars(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.created_at.desc())
    ).first()
    ev.created_at = when
    session.commit()


# --- single ack endpoint -----------------------------------------------------


async def test_ack_endpoint_marks_seen(client, session, profile):
    t = _review_ticket(session, profile)
    r = await client.post(f"/api/v1/tickets/{t.id}/ack")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["acknowledged_at"] is not None
    assert body["acknowledged_by"] == "admin"


async def test_ack_endpoint_rejects_active_ticket(client, session, profile):
    t = create_ticket(session, title="t", profile_id=profile.id, source_path="/tmp")
    r = await client.post(f"/api/v1/tickets/{t.id}/ack")
    assert r.status_code == 409, r.text


async def test_ack_endpoint_404(client):
    r = await client.post("/api/v1/tickets/nope/ack")
    assert r.status_code == 404


# --- agent_reviewed / archived_at garnishes ----------------------------------


async def test_agent_reviewed_chip_flag(client, session, profile):
    agent = _review_ticket(session, profile, actor=run_actor("r1"))
    human = _review_ticket(session, profile, actor=ADMIN)
    a = (await client.get(f"/api/v1/tickets/{agent.id}")).json()
    h = (await client.get(f"/api/v1/tickets/{human.id}")).json()
    assert a["agent_reviewed"] is True
    assert h["agent_reviewed"] is False


async def test_archived_at_from_events(client, session, profile):
    t = _review_ticket(session, profile)
    transition_status(session, t.id, "archived", actor=run_actor("r1"))
    body = (await client.get(f"/api/v1/tickets/{t.id}")).json()
    assert body["archived_at"] is not None


# --- acknowledged filter on list --------------------------------------------


async def test_acknowledged_filter(client, session, profile):
    unacked = _review_ticket(session, profile)
    acked = _review_ticket(session, profile)
    await client.post(f"/api/v1/tickets/{acked.id}/ack")

    r = await client.get("/api/v1/tickets", params={"status": "review", "acknowledged": "false"})
    ids = {t["id"] for t in r.json()}
    assert unacked.id in ids and acked.id not in ids

    r = await client.get("/api/v1/tickets", params={"status": "review", "acknowledged": "true"})
    ids = {t["id"] for t in r.json()}
    assert acked.id in ids and unacked.id not in ids


# --- bulk ack ----------------------------------------------------------------


async def test_bulk_ack_by_ids(client, session, profile):
    a = _review_ticket(session, profile)
    b = _review_ticket(session, profile)
    r = await client.post("/api/v1/tickets/ack", json={"ticket_ids": [a.id, b.id]})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    assert (await client.get(f"/api/v1/tickets/{a.id}")).json()["acknowledged_at"] is not None


async def test_bulk_ack_by_project(client, session, profile):
    proj = create_project(session, name="Proj", source_path="/tmp")
    other = create_project(session, name="Other", source_path="/tmp")
    p1 = _review_ticket(session, profile, project_id=proj.id)
    p2 = _review_ticket(session, profile, project_id=proj.id)
    q1 = _review_ticket(session, profile, project_id=other.id)

    r = await client.post(
        "/api/v1/tickets/ack",
        json={"project_scope": True, "project_id": proj.id},
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["acknowledged"]) == {p1.id, p2.id}
    assert (await client.get(f"/api/v1/tickets/{q1.id}")).json()["acknowledged_at"] is None


async def test_bulk_ack_before_is_race_safe(client, session, profile):
    old = _review_ticket(session, profile)
    fresh = _review_ticket(session, profile)
    now = datetime.now(timezone.utc)
    _backdate_entering(session, old.id, now - timedelta(hours=2))
    _backdate_entering(session, fresh.id, now + timedelta(hours=2))  # "arrived mid-read"

    r = await client.post(
        "/api/v1/tickets/ack",
        json={"project_scope": True, "project_id": None, "before": now.isoformat()},
    )
    assert set(r.json()["acknowledged"]) == {old.id}
    assert (await client.get(f"/api/v1/tickets/{fresh.id}")).json()["acknowledged_at"] is None


async def test_bulk_ack_requires_a_mode(client):
    r = await client.post("/api/v1/tickets/ack", json={})
    assert r.status_code == 422


# --- digest ------------------------------------------------------------------


async def test_digest_count_endpoint(client, session, profile):
    _review_ticket(session, profile)
    _review_ticket(session, profile)
    r = await client.get("/api/v1/tickets/ack/count")
    assert r.json()["count"] == 2


async def test_digest_groups_by_project_and_day(client, session, profile):
    proj = create_project(session, name="P", source_path="/tmp")
    now = datetime.now(timezone.utc)

    today = _review_ticket(session, profile, project_id=proj.id)
    yest1 = _review_ticket(session, profile, project_id=proj.id)
    yest2 = _review_ticket(session, profile, project_id=proj.id)
    _backdate_entering(session, yest1.id, now - timedelta(days=1))
    _backdate_entering(session, yest2.id, now - timedelta(days=1))

    r = await client.get("/api/v1/tickets/ack/digest")
    body = r.json()
    assert body["total"] == 3
    # Two groups (today, yesterday), newest first.
    assert len(body["groups"]) == 2
    assert body["groups"][0]["count"] == 1   # today
    assert body["groups"][1]["count"] == 2   # yesterday
    assert body["generated_at"] is not None


async def test_digest_excludes_acked(client, session, profile):
    t = _review_ticket(session, profile)
    await client.post(f"/api/v1/tickets/{t.id}/ack")
    r = await client.get("/api/v1/tickets/ack/digest")
    assert r.json()["total"] == 0
