"""Tests for the list view surface.

Covers:
  * rendering (page shell, column headers, grouped rows, inline pickers)
  * query/display parity with the board (same filtered ticket set)
  * grouping + ordering display settings
  * the focused-cursor contract (rows carry data-ticket-id under section[data-column])
  * last-run status column
  * the rows fragment route (poll/refresh target)
  * pure grouping/ordering helpers in domain.display
"""
import re

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.domain import display
from nightdesk.domain.labels import create_label, set_ticket_labels
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import create_ticket


@pytest.fixture
def app(engine, tmp_path):
    from nightdesk.api.app import create_app

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
        transport=transport, base_url="http://test", cookies={"nightdesk_token": "t"}
    ) as ac:
        yield ac


@pytest.fixture
def profile(session):
    return create_profile(
        session, name="list-test", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


def _mk(session, profile, title, status="draft", **kw):
    return create_ticket(
        session, title=title, prompt="x", profile_id=profile.id,
        status=status, source_path="/tmp", **kw,
    )


def _row_ids(text):
    """Ticket ids rendered as actual board cards / list rows.

    Keys off ``data-ticket-id`` (emitted only by ticket_card.html /
    list_row.html), so it ignores ticket data that the create modal's
    dependency picker embeds for *every* ticket.
    """
    return set(re.findall(r'data-ticket-id="([^"]+)"', text))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


async def test_list_page_renders(cookie_client, session, profile):
    _mk(session, profile, "alpha", "draft")
    _mk(session, profile, "bravo", "review")
    r = await cookie_client.get("/list")
    assert r.status_code == 200
    body = r.text
    assert "alpha" in body and "bravo" in body
    # Column headers for the important properties.
    for header in ("Status", "Priority", "Project", "Profile", "Last run", "Updated"):
        assert header in body, f"missing list header {header!r}"


async def test_list_rows_carry_cursor_and_group_structure(cookie_client, session, profile):
    t = _mk(session, profile, "cursor-me", "draft")
    r = await cookie_client.get("/list")
    body = r.text
    # Same focused-cursor contract as board rows: data-ticket-id under
    # section[data-column], plus the #board-grid gate command_palette.js checks.
    assert f'data-ticket-id="{t.id}"' in body
    assert "data-column=" in body
    assert 'id="board-grid"' in body


async def test_list_rows_have_inline_property_pickers(cookie_client, session, profile):
    t = _mk(session, profile, "pick-me", "draft", priority=2)
    body = (await cookie_client.get("/list")).text
    # The shared picker primitive is reused for status/priority/project.
    for prop in ("status", "priority", "project"):
        assert f'data-property-picker="{t.id}:{prop}"' in body, f"missing {prop} picker"


# --------------------------------------------------------------------------- #
# Query / display parity with the board
# --------------------------------------------------------------------------- #


async def test_list_matches_board_ticket_set(cookie_client, session, profile):
    ids = {}
    for title, status in [
        ("draft-t", "draft"), ("queued-t", "queued"),
        ("review-t", "review"), ("archived-t", "archived"),
    ]:
        ids[title] = _mk(session, profile, title, status).id

    board_ids = _row_ids((await cookie_client.get("/")).text)
    list_ids = _row_ids((await cookie_client.get("/list")).text)

    # The board excludes archived tickets; the list must show the exact same
    # set of rows as the board.
    active = {ids["draft-t"], ids["queued-t"], ids["review-t"]}
    assert board_ids == active
    assert list_ids == board_ids
    assert ids["archived-t"] not in list_ids


async def test_list_honours_query_filter_like_board(cookie_client, session, profile):
    draft_id = _mk(session, profile, "only-draft", "draft").id
    review_id = _mk(session, profile, "only-review", "review").id

    board_ids = _row_ids((await cookie_client.get("/?q=status%3Ddraft")).text)
    list_ids = _row_ids((await cookie_client.get("/list?q=status%3Ddraft")).text)

    assert board_ids == {draft_id}
    assert list_ids == board_ids
    assert review_id not in list_ids


async def test_list_rows_fragment_is_partial(cookie_client, session, profile):
    _mk(session, profile, "frag-t", "draft")
    r = await cookie_client.get("/board/list-rows")
    assert r.status_code == 200
    assert "frag-t" in r.text
    # Fragment only — no page shell, so the sidebar editor survives refreshes.
    assert "<html" not in r.text.lower()
    assert "data-list-root" in r.text


# --------------------------------------------------------------------------- #
# Grouping + ordering
# --------------------------------------------------------------------------- #


async def test_group_by_project_shows_project_headers(cookie_client, session, profile):
    proj = create_project(session, name="Acme", source_path="/tmp")
    _mk(session, profile, "in-acme", "draft", project_id=proj.id)
    _mk(session, profile, "no-proj", "draft")
    body = (await cookie_client.get("/list?group=project")).text
    assert "Acme" in body
    assert "No project" in body
    assert "in-acme" in body and "no-proj" in body


async def test_group_by_priority(cookie_client, session, profile):
    _mk(session, profile, "urgent-one", "draft", priority=4)
    _mk(session, profile, "low-one", "draft", priority=1)
    body = (await cookie_client.get("/list?group=priority")).text
    assert "Urgent" in body and "Low" in body
    # Urgent group renders before the Low group.
    assert body.index("Urgent") < body.index("Low")


async def test_order_by_title(cookie_client, session, profile):
    # Insert out of alphabetical order; ordering should sort within the group.
    _mk(session, profile, "zeta", "draft")
    _mk(session, profile, "alpha", "draft")
    body = (await cookie_client.get("/list?order=title&group=none")).text
    assert body.index("alpha") < body.index("zeta")


async def test_group_none_is_flat(cookie_client, session, profile):
    _mk(session, profile, "flat-a", "draft")
    _mk(session, profile, "flat-b", "review")
    body = (await cookie_client.get("/list?group=none")).text
    assert "flat-a" in body and "flat-b" in body


# --------------------------------------------------------------------------- #
# Properties: labels + last-run status
# --------------------------------------------------------------------------- #


async def test_list_shows_labels(cookie_client, session, profile):
    t = _mk(session, profile, "labelled", "draft")
    lab = create_label(session, name="backend", color="#ff0000")
    set_ticket_labels(session, t.id, [lab.id])
    body = (await cookie_client.get("/list")).text
    assert "backend" in body


async def test_list_shows_last_run_status(cookie_client, session, profile):
    t = _mk(session, profile, "ran-ok", "review")
    run = start_run(
        session, ticket_id=t.id, worktree_path="/tmp/wt",
        transcript_path="/tmp/tr", pid=None, host="h",
    )
    finish_run(session, run.id, exit_status="success", error_summary=None)
    body = (await cookie_client.get("/list")).text
    assert "success" in body


# --------------------------------------------------------------------------- #
# domain.display unit tests
# --------------------------------------------------------------------------- #


def test_normalize_group_and_order_defaults():
    assert display.normalize_group("bogus") == "status"
    assert display.normalize_group("PROJECT") == "project"
    assert display.normalize_order("") == "manual"
    assert display.normalize_order("Title") == "title"


class _T:
    def __init__(self, title, priority=0):
        self.title = title
        self.priority = priority


def test_order_tickets_title_and_priority():
    items = [_T("b", 1), _T("a", 3), _T("c", 2)]
    assert [t.title for t in display.order_tickets(items, "title")] == ["a", "b", "c"]
    assert [t.priority for t in display.order_tickets(items, "priority")] == [3, 2, 1]
    # 'manual' preserves incoming order.
    assert [t.title for t in display.order_tickets(items, "manual")] == ["b", "a", "c"]


def test_group_tickets_priority_buckets_descending():
    items = [_T("low", 1), _T("urgent", 4), _T("mid", 2)]
    groups = display.group_tickets(items, group="priority", order="manual")
    labels = [g["label"] for g in groups]
    assert labels[0] == "Urgent"  # highest priority first
    assert all(g["count"] == 1 for g in groups)
