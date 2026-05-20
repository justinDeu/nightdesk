from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

import nightdesk.api.routes.ticket_page as ticket_page_routes
from nightdesk.db.models import Run
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket


async def test_runs_empty_initially(client):
    r = await client.get("/api/v1/runs")
    assert r.status_code == 200
    assert r.json() == []


@pytest.fixture
async def cookie_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


def _mk_run(session) -> str:
    profile = create_profile(
        session, name="p1", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="t", prompt="hi", priority=0,
                      profile_id=profile.id, run_now=False,
                      status="queued", cwd="/tmp")
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc),
        worktree_path="/tmp/wt",
        transcript_path="/tmp/tr",
        host="testhost",
    )
    session.add(run)
    session.commit()
    return run.id


async def test_cookie_client_downloads_run_log(cookie_client, session, tmp_path, monkeypatch):
    """Browser cookie session can pull the worker log via the new route.

    Regression for the run-log download button, which pointed at the
    bearer-only /api/v1 route and 401'd for cookie sessions.
    """
    rid = _mk_run(session)
    log_file = tmp_path / f"{rid}.log"
    log_file.write_text("hello from the worker log\n")
    monkeypatch.setattr(
        ticket_page_routes, "run_log_path", lambda run_id: log_file,
    )

    r = await cookie_client.get(f"/runs/{rid}/log")
    assert r.status_code == 200
    assert "hello from the worker log" in r.text
