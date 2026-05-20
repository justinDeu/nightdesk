"""Smoke tests for the header bar HTMX endpoints.

These hit ``/header/worker-pill`` (and later ``/header/search``) and verify
the rendered HTML contains the expected pieces. They use cookie-based auth
(the flow the real browser uses) since header endpoints are guarded by
``require_token_cookie_or_bearer``.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from nightdesk.api.app import create_app
from nightdesk.db.models import Profile, Run, Ticket, WorkerHeartbeat


@pytest.fixture
def app(engine, tmp_path):
    return create_app(engine=engine, bearer_token="t",
                       static_root=tmp_path / "static",
                       transcript_root=tmp_path / "transcripts",
                       worktree_root=tmp_path / "work")


@pytest.fixture
async def cookie_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                              cookies={"nightdesk_token": "t"}) as ac:
        yield ac


def _setup_fts(session):
    """Create the FTS5 virtual table the search backend reads from.

    The migration installs this in production; the in-memory test engine
    uses ``Base.metadata.create_all`` which doesn't create FTS tables.
    """
    session.execute(text(
        "CREATE VIRTUAL TABLE IF NOT EXISTS tickets_fts USING fts5("
        "title, prompt, id UNINDEXED)"
    ))
    session.commit()


def _seed_searchable_ticket(session, *, tid, title, prompt=""):
    p = session.query(Profile).first()
    if p is None:
        p = Profile(name="hp", fs_read=[], fs_write=[], allowed_tools=[],
                     denied_tools=[], network_mode="off",
                     network_allowlist=[], secret_keys=[])
        session.add(p)
        session.commit()
    t = Ticket(id=tid, title=title, prompt=prompt,
                status="draft", priority=0, position=0, profile_id=p.id,
                additional_dirs=[], cwd="/tmp", run_now=False)
    session.add(t)
    session.commit()
    session.execute(text(
        "INSERT INTO tickets_fts(rowid, title, prompt, id) "
        "VALUES ((SELECT rowid FROM tickets WHERE id=:id), :title, :prompt, :id)"
    ), {"id": tid, "title": title, "prompt": prompt})
    session.commit()
    return t


async def test_header_search_with_hits(cookie_client, session):
    _setup_fts(session)
    _seed_searchable_ticket(session, tid="t1", title="hello world ticket")
    r = await cookie_client.get("/header/search", params={"q": "hello"})
    assert r.status_code == 200
    assert "hello world ticket" in r.text
    assert "/tickets/t1" in r.text


async def test_header_search_empty_query_renders_empty_state(cookie_client):
    r = await cookie_client.get("/header/search", params={"q": ""})
    assert r.status_code == 200
    assert "/tickets/" not in r.text
    assert "Type to search" in r.text


async def test_header_search_short_query_skipped(cookie_client, session):
    _setup_fts(session)
    _seed_searchable_ticket(session, tid="t1", title="alpha")
    # 1-char queries are skipped (min length 2) to avoid noisy results.
    r = await cookie_client.get("/header/search", params={"q": "a"})
    assert r.status_code == 200
    assert "/tickets/t1" not in r.text


async def test_header_search_no_hits_renders_no_matches(cookie_client, session):
    _setup_fts(session)
    _seed_searchable_ticket(session, tid="t1", title="alpha")
    r = await cookie_client.get("/header/search", params={"q": "zzznomatch"})
    assert r.status_code == 200
    assert "No matches" in r.text


async def test_header_search_requires_auth(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/header/search", params={"q": "x"})
    assert r.status_code == 401


async def test_base_template_includes_search_input(cookie_client):
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert 'id="header-search"' in r.text
    assert 'id="search-results"' in r.text
    assert 'hx-get="/header/search"' in r.text


async def test_worker_pill_offline_when_no_heartbeat(cookie_client):
    r = await cookie_client.get("/header/worker-pill")
    assert r.status_code == 200
    assert "offline" in r.text
    assert "bg-danger" in r.text


async def test_worker_pill_shows_host_and_counts_when_alive(cookie_client, session):
    hb = WorkerHeartbeat(id=1, host="thorpad", pid=4242,
                          last_seen_at=datetime.now(timezone.utc))
    session.add(hb)
    session.commit()
    p = Profile(name="wp", fs_read=[], fs_write=[], allowed_tools=[],
                 denied_tools=[], network_mode="off", network_allowlist=[],
                 secret_keys=[])
    session.add(p)
    session.commit()
    t = Ticket(id="rt", title="running", prompt="",
                status="running", priority=0, position=0, profile_id=p.id,
                additional_dirs=[], cwd="/tmp", run_now=False)
    session.add(t)
    session.commit()
    # v2: the pill counts unfinished Run rows, not Ticket.status='running'.
    # Tickets stuck in 'running' without a Run row are a separate bug
    # (orphan recovery handles them) — they no longer inflate the pill.
    run = Run(id="rt-run", ticket_id="rt",
                started_at=datetime.now(timezone.utc),
                worktree_path="/tmp/w", transcript_path="/tmp/t",
                host="thorpad", started_as_run_now=False)
    session.add(run)
    t.current_run_id = run.id
    session.commit()

    r = await cookie_client.get("/header/worker-pill")
    assert r.status_code == 200
    assert "thorpad" in r.text
    assert "1/" in r.text
    assert "offline" not in r.text


async def test_worker_pill_run_now_overflow_indicator(cookie_client, session):
    hb = WorkerHeartbeat(id=1, host="thorpad", pid=4242,
                          last_seen_at=datetime.now(timezone.utc))
    session.add(hb)
    p = Profile(name="wp2", fs_read=[], fs_write=[], allowed_tools=[],
                 denied_tools=[], network_mode="off", network_allowlist=[],
                 secret_keys=[])
    session.add(p)
    session.commit()
    t = Ticket(id="rt2", title="run-now ticket", prompt="",
                status="running", priority=0, position=0, profile_id=p.id,
                additional_dirs=[], cwd="/tmp", run_now=True)
    session.add(t)
    session.commit()
    run = Run(id="r-now", ticket_id="rt2",
                started_at=datetime.now(timezone.utc),
                worktree_path="/tmp/w", transcript_path="/tmp/t",
                host="thorpad", started_as_run_now=True)
    session.add(run)
    session.commit()
    t.current_run_id = run.id
    session.commit()

    r = await cookie_client.get("/header/worker-pill")
    assert r.status_code == 200
    assert "+1" in r.text


async def test_worker_pill_requires_auth(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/header/worker-pill")
    assert r.status_code == 401


async def test_base_template_includes_worker_pill_polling(cookie_client):
    # Smoke: extending templates should not crash with the new header slots
    # filled in. /dashboard extends base.html and needs no fixtures.
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert 'id="worker-pill"' in r.text
    assert 'hx-get="/header/worker-pill"' in r.text


async def test_search_box_precedes_nav_in_header_markup(cookie_client):
    """Regression guard: #header-search must appear to the LEFT of the <nav>
    links in the rendered HTML. If the order is ever swapped back the search
    box will drift to the right side of the bar and this test will catch it."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    body = r.text
    search_pos = body.index('id="header-search"')
    nav_pos = body.index("<nav ")
    assert search_pos < nav_pos, (
        "#header-search must appear before <nav> in the header markup "
        f"(search at {search_pos}, nav at {nav_pos})"
    )
