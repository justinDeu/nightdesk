"""Route tests for the unified search bar surfaces.

Structured queries (field=value) are used throughout so these don't need the
FTS virtual table, which the in-memory test engine doesn't install.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import create_ticket


@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        engine=engine,
        bearer_token="t",
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


@pytest.fixture
def profile(session):
    return create_profile(
        session, name="searchbar", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[], secret_keys=[],
        backend="claude_sdk",
    )


def _failed_run(session, ticket, *, model="claude-opus-4-x"):
    r = start_run(session, ticket_id=ticket.id, worktree_path="/wt",
                  transcript_path="/tr", pid=None, host="h")
    finish_run(session, r.id, exit_status="failed", error_summary=None)
    r.model_used = model
    session.commit()
    return r


# ---- /search/suggest ------------------------------------------------------ #
async def test_suggest_status_values(cookie_client):
    r = await cookie_client.get("/search/suggest?field=status")
    assert r.status_code == 200
    values = [v["value"] for v in r.json()["values"]]
    assert "review" in values and "draft" in values


async def test_suggest_status_runs_resource_returns_outcomes(cookie_client):
    r = await cookie_client.get("/search/suggest?field=status&resource=run")
    values = [v["value"] for v in r.json()["values"]]
    assert "failed" in values and "success" in values
    assert "draft" not in values


async def test_suggest_project_lists_slugs(cookie_client, session, profile):
    create_project(session, name="Nightdesk", source_path="/tmp/nd")
    r = await cookie_client.get("/search/suggest?field=project")
    pairs = {(v["value"], v["label"]) for v in r.json()["values"]}
    assert ("none", "No project") in pairs
    assert ("nightdesk", "Nightdesk") in pairs


async def test_suggest_prefix_filters(cookie_client, session, profile):
    create_project(session, name="Nightdesk", source_path="/tmp/nd")
    create_project(session, name="Omc", source_path="/tmp/omc")
    r = await cookie_client.get("/search/suggest?field=project&q=omc")
    values = [v["value"] for v in r.json()["values"]]
    assert values == ["omc"]


# ---- board filtering ------------------------------------------------------ #
async def test_board_filters_by_status_query(cookie_client, session, profile):
    create_ticket(session, title="keep-review", prompt="x", profile_id=profile.id,
                  status="review", source_path="/tmp")
    create_ticket(session, title="hide-draft", prompt="x", profile_id=profile.id,
                  status="draft", source_path="/tmp")
    # The columns fragment renders only ticket cards (no dep-picker payload),
    # so it cleanly reflects the query filter.
    r = await cookie_client.get("/board/columns?q=status%3Dreview")
    assert r.status_code == 200
    assert "keep-review" in r.text
    assert "hide-draft" not in r.text


async def test_board_search_bar_prefilled_with_query(cookie_client):
    r = await cookie_client.get("/?q=status%3Dreview")
    # The query is carried on the bar; search_bar.js renders it as inline chips.
    assert 'data-query="status=review"' in r.text
    assert 'data-nd-searchbar' in r.text


async def test_legacy_project_param_becomes_query(cookie_client, session, profile):
    project = create_project(session, name="Nightdesk", source_path="/tmp/nd")
    create_ticket(session, title="nd-ticket", prompt="x", profile_id=profile.id,
                  status="draft", source_path="/tmp/nd", project_id=project.id)
    r = await cookie_client.get("/?project=nightdesk")
    assert 'data-query="project=nightdesk"' in r.text
    assert "nd-ticket" in r.text


# ---- header / palette search (query language) ----------------------------- #
async def test_header_search_filters_by_field(cookie_client, session, profile):
    create_ticket(session, title="alpha task", prompt="x", profile_id=profile.id,
                  status="review", source_path="/tmp")
    create_ticket(session, title="beta task", prompt="x", profile_id=profile.id,
                  status="draft", source_path="/tmp")
    r = await cookie_client.get("/header/search?q=status%3Dreview")
    assert r.status_code == 200
    assert "alpha task" in r.text
    assert "beta task" not in r.text


# ---- runs view ------------------------------------------------------------ #
async def test_board_runs_fragment_filters_by_outcome(cookie_client, session, profile):
    t1 = create_ticket(session, title="failing-run-ticket", prompt="x",
                        profile_id=profile.id, status="review", source_path="/tmp")
    t2 = create_ticket(session, title="ok-run-ticket", prompt="x",
                        profile_id=profile.id, status="review", source_path="/tmp")
    _failed_run(session, t1)
    ok = start_run(session, ticket_id=t2.id, worktree_path="/wt",
                   transcript_path="/tr", pid=None, host="h")
    finish_run(session, ok.id, exit_status="success", error_summary=None)

    r = await cookie_client.get("/board/runs?q=outcome%3Dfailed")
    assert r.status_code == 200
    assert "failing-run-ticket" in r.text
    assert "ok-run-ticket" not in r.text


async def test_board_view_runs_initial_render(cookie_client, session, profile):
    t = create_ticket(session, title="run-host-ticket", prompt="x",
                      profile_id=profile.id, status="review", source_path="/tmp")
    _failed_run(session, t)
    r = await cookie_client.get("/?view=runs")
    assert r.status_code == 200
    # Runs table present, kanban grid hidden.
    assert 'id="board-runs-table"' in r.text
    assert "run-host-ticket" in r.text
