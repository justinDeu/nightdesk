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


# --------------------------------------------------------------------------- #
# Bulk action bar on list page
# --------------------------------------------------------------------------- #


async def test_list_bulk_bar_renders_on_list(cookie_client, session, profile):
    """The bulk action bar is included in the list page with all required
    option sources: priority scale, statuses, projects, labels, profiles."""
    proj = create_project(session, name="ListProj", source_path="/tmp/lp")
    create_label(session, name="list-label", color="#abcdef")
    _mk(session, profile, "bar-row", "draft", project_id=proj.id)
    r = await cookie_client.get("/list")
    assert r.status_code == 200
    html = r.text
    # Bar element present.
    assert 'id="nd-bulk-bar"' in html
    # All menu toggles present.
    for toggle in ("priority", "status", "project", "labels", "profile"):
        assert f'data-nd-bulk-toggle="{toggle}"' in html, \
            f"missing {toggle} toggle in list bulk bar"
    # Archive + clear controls.
    assert "data-nd-bulk-archive" in html
    assert "data-nd-bulk-clear" in html
    # Option sources rendered server-side.
    assert "Urgent" in html          # from PRIORITY_SCALE
    assert "draft" in html           # from bulk_statuses
    assert "ListProj" in html        # project option
    assert "list-label" in html      # label option
    assert profile.name in html      # profile option


async def test_list_bulk_bar_profile_menu_lists_profiles(
    cookie_client, session, profile
):
    """The Profile menu in the bulk bar shows all available profiles."""
    r = await cookie_client.get("/list")
    assert r.status_code == 200
    html = r.text
    assert 'data-nd-bulk-toggle="profile"' in html
    assert f'data-nd-bulk-apply="profile"' in html
    assert profile.name in html


# --------------------------------------------------------------------------- #
# Group-by-label on the list
# --------------------------------------------------------------------------- #


async def test_group_by_label_list(cookie_client, session, profile):
    """?group=label shows a group header per label plus Unlabeled."""
    lab = create_label(session, name="infra", color="#ff0000")
    t1 = _mk(session, profile, "labelled-t", "draft")
    t2 = _mk(session, profile, "unlabelled-t", "draft")
    set_ticket_labels(session, t1.id, [lab.id])
    body = (await cookie_client.get("/list?group=label")).text
    assert "infra" in body
    assert "Unlabeled" in body
    assert t1.id in body
    assert t2.id in body


async def test_group_by_label_list_multi_label_membership(
    cookie_client, session, profile
):
    """A ticket with two labels appears under each label group."""
    l1 = create_label(session, name="backend", color="#00ff00")
    l2 = create_label(session, name="frontend", color="#0000ff")
    t = _mk(session, profile, "multi", "draft")
    set_ticket_labels(session, t.id, [l1.id, l2.id])
    body = (await cookie_client.get("/list?group=label")).text
    # The ticket id appears at least twice in the rendered HTML (once per label).
    assert body.count(f'data-ticket-id="{t.id}"') >= 2


async def test_group_by_label_list_unlabeled_bucket(
    cookie_client, session, profile
):
    """Tickets without any labels land in the Unlabeled group."""
    create_label(session, name="tag", color="#aabbcc")
    t = _mk(session, profile, "bare", "draft")
    body = (await cookie_client.get("/list?group=label")).text
    assert "Unlabeled" in body
    assert f'data-ticket-id="{t.id}"' in body


async def test_group_label_in_toolbar_select(cookie_client, session, profile):
    """The Label option appears in the group-by toolbar select element."""
    body = (await cookie_client.get("/list")).text
    assert '<option value="label"' in body


async def test_group_label_selected_when_active(cookie_client, session, profile):
    """When ?group=label is active the Label option is marked selected."""
    body = (await cookie_client.get("/list?group=label")).text
    assert 'value="label" selected' in body


# --------------------------------------------------------------------------- #
# pickerOpen guard includes bulk menu check
# --------------------------------------------------------------------------- #


async def test_picker_open_guard_includes_bulk_menu(cookie_client, session, profile):
    """list.html's pickerOpen() guard must include the bulk-menu selector so an
    open bulk menu suppresses the 4 s poll."""
    body = (await cookie_client.get("/list")).text
    # The guard expression must contain the bulk-menu check (verified via the
    # rendered script tag, not runtime execution).
    assert "[data-nd-bulk-menu]:not([hidden])" in body


# --------------------------------------------------------------------------- #
# domain.display label grouping unit tests
# --------------------------------------------------------------------------- #


class _LabelStub:
    def __init__(self, id_, name, color=""):
        self.id = id_
        self.name = name
        self.color = color


class _TWithLabels:
    def __init__(self, title, labels=()):
        self.title = title
        self.labels = list(labels)
        self.priority = 0


def test_label_in_list_group_options():
    keys = [k for k, _ in display.LIST_GROUP_OPTIONS]
    assert "label" in keys


def test_group_by_label_multi_membership():
    la = _LabelStub("la", "alpha")
    lb = _LabelStub("lb", "beta")
    t1 = _TWithLabels("t1", [la, lb])
    t2 = _TWithLabels("t2", [la])
    t3 = _TWithLabels("t3", [])
    groups = display.group_tickets(
        [t1, t2, t3], group="label", order="manual", labels=[la, lb]
    )
    keys = [g["key"] for g in groups]
    assert "label:la" in keys
    assert "label:lb" in keys
    assert "label:__none__" in keys
    alpha = next(g for g in groups if g["key"] == "label:la")
    beta = next(g for g in groups if g["key"] == "label:lb")
    unlabeled = next(g for g in groups if g["key"] == "label:__none__")
    # t1 appears in both alpha and beta; t2 only in alpha.
    assert t1 in alpha["tickets"] and t1 in beta["tickets"]
    assert t2 in alpha["tickets"] and t2 not in beta["tickets"]
    # t3 (no labels) lands in Unlabeled.
    assert t3 in unlabeled["tickets"]
    assert t3 not in alpha["tickets"]


def test_group_by_label_swatch_color():
    la = _LabelStub("la", "alpha", "#abcdef")
    t = _TWithLabels("t", [la])
    groups = display.group_tickets([t], group="label", order="manual", labels=[la])
    alpha = next(g for g in groups if g["key"] == "label:la")
    assert alpha["swatch_color"] == "#abcdef"


def test_group_by_label_unlabeled_last():
    la = _LabelStub("la", "alpha")
    t1 = _TWithLabels("t1", [la])
    t2 = _TWithLabels("t2", [])
    groups = display.group_tickets(
        [t1, t2], group="label", order="manual", labels=[la]
    )
    assert groups[-1]["key"] == "label:__none__"
