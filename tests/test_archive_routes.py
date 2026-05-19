"""Smoke tests for the v2 archive page."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.db.models import Run
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import (
    archive as archive_ticket,
    create_ticket,
    transition_status,
)


@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        engine=engine, bearer_token="t",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )


@pytest.fixture
async def cookie_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


def _mk_profile(session, name: str = "p1"):
    return create_profile(
        session, name=name, fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )


def _archive_ticket(session, *, profile_id: str, title: str = "archived ticket",
                     outcome: str = "success") -> str:
    """Create a ticket, run it through the lifecycle, finish the run, archive it."""
    t = create_ticket(session, title=title, prompt="hi",
                       priority=0, profile_id=profile_id, run_now=False,
                       status="queued", cwd="/tmp")
    transition_status(session, t.id, "running")
    # Attach a finished run so the archive page has an outcome to show.
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        finished_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        exit_status=outcome,
        worktree_path="/tmp/wt",
        transcript_path="/tmp/tr",
        host="testhost",
    )
    session.add(run)
    session.flush()
    t.current_run_id = run.id
    session.commit()
    transition_status(session, t.id, "review")
    archive_ticket(session, t.id)
    return t.id


async def test_archive_page_renders_empty(cookie_client):
    r = await cookie_client.get("/archive")
    assert r.status_code == 200
    assert "Archive" in r.text
    assert "No archived tickets match these filters." in r.text
    # Filter inputs are present.
    assert 'name="outcome"' in r.text
    assert 'name="profile_id"' in r.text
    assert 'name="since"' in r.text
    assert 'name="until"' in r.text
    assert 'name="q"' in r.text


async def test_archive_page_lists_archived_ticket(cookie_client, session):
    p = _mk_profile(session)
    _archive_ticket(session, profile_id=p.id, title="my archived ticket")
    r = await cookie_client.get("/archive")
    assert r.status_code == 200
    assert "my archived ticket" in r.text
    assert "success" in r.text


async def test_archive_rows_partial(cookie_client, session):
    p = _mk_profile(session)
    _archive_ticket(session, profile_id=p.id, title="row partial ticket")
    r = await cookie_client.get("/archive/rows")
    assert r.status_code == 200
    # Should NOT include the full filter form — it's just the table partial.
    assert 'name="outcome"' not in r.text
    assert "row partial ticket" in r.text


async def test_archive_outcome_filter(cookie_client, session):
    p = _mk_profile(session)
    _archive_ticket(session, profile_id=p.id, title="good run", outcome="success")
    _archive_ticket(session, profile_id=p.id, title="bad run", outcome="failed")

    r = await cookie_client.get("/archive/rows", params={"outcome": "failed"})
    assert r.status_code == 200
    assert "bad run" in r.text
    assert "good run" not in r.text

    r = await cookie_client.get("/archive/rows", params={"outcome": "success"})
    assert "good run" in r.text
    assert "bad run" not in r.text


async def test_archive_profile_filter(cookie_client, session):
    p1 = _mk_profile(session, name="alpha")
    p2 = _mk_profile(session, name="beta")
    _archive_ticket(session, profile_id=p1.id, title="alpha-ticket")
    _archive_ticket(session, profile_id=p2.id, title="beta-ticket")

    r = await cookie_client.get("/archive/rows", params={"profile_id": p1.id})
    assert r.status_code == 200
    assert "alpha-ticket" in r.text
    assert "beta-ticket" not in r.text


async def test_unarchive_button_returns_ticket_to_queued(cookie_client, session):
    p = _mk_profile(session)
    tid = _archive_ticket(session, profile_id=p.id, title="bring me back")

    r = await cookie_client.post(f"/archive/tickets/{tid}/unarchive",
                                  follow_redirects=False)
    # 303 redirect back to /archive on success.
    assert r.status_code == 303
    assert r.headers["location"] == "/archive"

    # Sanity check that the domain transition actually happened.
    tr = await cookie_client.get(f"/api/v1/tickets/{tid}",
                                  headers={"Authorization": "Bearer t"})
    assert tr.status_code == 200
    assert tr.json()["status"] == "queued"


async def test_unarchive_nonexistent_404(cookie_client):
    r = await cookie_client.post("/archive/tickets/no-such-id/unarchive")
    assert r.status_code == 404


async def test_archive_requires_auth(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/archive")
    assert r.status_code == 401
