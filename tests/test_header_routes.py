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
from nightdesk.db.models import (
    Profile,
    Run,
    ScheduleWindow,
    Ticket,
    WorkerHeartbeat,
)


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
                additional_dirs=[], run_now=False)
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


async def test_base_template_has_no_header_search_box(cookie_client):
    """The header search box is retired; search lives in the command palette.

    /header/search itself stays mounted (the palette queries it), but the
    base template must no longer render the old input or results dropdown.
    """
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert 'id="header-search"' not in r.text
    assert 'id="search-results"' not in r.text
    assert "command_palette.html" not in r.text  # include resolved, not leaked
    assert 'id="nd-palette"' in r.text or "command_palette.js" in r.text


async def test_worker_pill_offline_when_no_heartbeat(cookie_client):
    r = await cookie_client.get("/header/worker-pill")
    assert r.status_code == 200
    assert "offline" in r.text
    assert "bg-danger" in r.text


def _add_always_on_window(session, *, max_parallel=2, label="default"):
    """An every-day, always-on schedule window so capacity is non-zero.

    Mirrors what the Settings UI writes for an "always on" window (equal
    start/end == always on; day_mask 127 == every day).
    """
    session.add(ScheduleWindow(label=label, day_mask=127, start="00:00",
                               end="00:00", max_parallel=max_parallel,
                               position=0))
    session.commit()


async def test_worker_pill_shows_host_and_counts_when_alive(cookie_client, session):
    hb = WorkerHeartbeat(id=1, host="thorpad", pid=4242,
                          last_seen_at=datetime.now(timezone.utc))
    session.add(hb)
    session.commit()
    # Capacity comes from configured schedule windows now, not the legacy
    # always-on ConfigRow default. Without a window there is no capacity.
    _add_always_on_window(session, max_parallel=2)
    p = Profile(name="wp", fs_read=[], fs_write=[], allowed_tools=[],
                 denied_tools=[], network_mode="off", network_allowlist=[],
                 secret_keys=[])
    session.add(p)
    session.commit()
    t = Ticket(id="rt", title="running", prompt="",
                status="running", priority=0, position=0, profile_id=p.id,
                additional_dirs=[], run_now=False)
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
                additional_dirs=[], run_now=True)
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


async def test_worker_pill_fresh_install_no_windows_shows_honest_state(
        cookie_client, session):
    """Issue A: a live worker with no schedule windows configured (fresh
    install) must NOT advertise a misleading "0/M" capacity fraction. Capacity
    is genuinely 0, so the pill shows the honest "no active window" state."""
    session.add(WorkerHeartbeat(id=1, host="thorpad", pid=4242,
                                last_seen_at=datetime.now(timezone.utc)))
    session.commit()
    # No ScheduleWindow rows at all => no capacity.
    r = await cookie_client.get("/header/worker-pill")
    assert r.status_code == 200
    assert "no active window" in r.text
    # The misleading capacity fraction must not appear.
    assert "0/" not in r.text


async def test_worker_pill_populates_active_window(cookie_client, session):
    """Issue B: when a schedule window is active, the popup reports its label
    rather than "none". The label is computed in _collect_worker_status."""
    session.add(WorkerHeartbeat(id=1, host="thorpad", pid=4242,
                                last_seen_at=datetime.now(timezone.utc)))
    session.commit()
    _add_always_on_window(session, max_parallel=3, label="graveyard")
    r = await cookie_client.get("/header/worker-pill")
    assert r.status_code == 200
    # Active-window label surfaces in the hover panel...
    assert "graveyard" in r.text
    # ...and the "none" placeholder is gone.
    assert ">none<" not in r.text
    # Honest capacity fraction (nothing running yet, real cap of 3).
    assert "0/3" in r.text


async def test_worker_pill_hover_panel_has_bridge_not_dead_gap(
        cookie_client, session):
    """Issue C: the hover panel must sit flush against the trigger (top-full,
    no margin) and use a pt-1 hover bridge so the mouse path from trigger to
    popup never crosses dead space. Guards against reintroducing the mt-1 gap."""
    session.add(WorkerHeartbeat(id=1, host="thorpad", pid=4242,
                                last_seen_at=datetime.now(timezone.utc)))
    session.commit()
    r = await cookie_client.get("/header/worker-pill")
    assert r.status_code == 200
    # The panel keeps group-hover behavior...
    assert "group-hover:block" in r.text
    # ...but the dead-gap margin is gone, replaced by a flush + padding bridge.
    assert "mt-1" not in r.text
    assert "top-full pt-1" in r.text


async def _seed_today_spend(session):
    """Heartbeat + a finished priced run so today's spend is non-zero."""
    from nightdesk.db.models import ConfigRow
    session.add(WorkerHeartbeat(id=1, host="thorpad", pid=1,
                                last_seen_at=datetime.now(timezone.utc)))
    cfg = session.get(ConfigRow, 1)
    if cfg is None:
        cfg = ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t")
        session.add(cfg)
    session.commit()
    p = Profile(name="bw", fs_read=[], fs_write=[], allowed_tools=[],
                denied_tools=[], network_mode="off", network_allowlist=[],
                secret_keys=[])
    session.add(p); session.commit()
    t = Ticket(id="bt", title="t", prompt="", status="review", priority=0,
               position=0, profile_id=p.id, additional_dirs=[],
               run_now=False)
    session.add(t); session.commit()
    session.add(Run(id="br", ticket_id="bt", started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc), exit_status="success",
                    worktree_path="/w", transcript_path="/x", host="h",
                    cost_usd=2.5))
    session.commit()


async def test_spend_chip_shows_today_spend(cookie_client, session):
    await _seed_today_spend(session)
    r = await cookie_client.get("/header/spend-chip")
    assert r.status_code == 200
    assert "2.50" in r.text
    assert "today" in r.text


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
