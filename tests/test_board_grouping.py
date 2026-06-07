"""Tests for the grouped board renderer (status / label / project / priority).

Covers the four groupings, group counts and empty-state behaviour, the
read-only (non-droppable) nature of property groupings vs the status kanban's
status-changing drag, the polled OOB fragment honouring the active grouping,
and grouping x query interactions.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.labels import create_label, set_ticket_labels
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
        transport=transport,
        base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


@pytest.fixture
def profile(session):
    return create_profile(
        session,
        name="group-test",
        fs_read=[],
        fs_write=[],
        allowed_tools=[],
        denied_tools=[],
        network_mode="off",
        network_allowlist=[],
        secret_keys=[],
        default_model=None,
    )


def _section_html(body: str, key: str) -> str:
    """Slice the ``<section id="board-col-<key>">`` block from a response."""
    marker = f'id="board-col-{key}"'
    start = body.find(marker)
    if start == -1:
        return ""
    open_tag = body.rfind("<section", 0, start)
    close = body.find("</section>", start)
    if open_tag == -1 or close == -1:
        return ""
    return body[open_tag:close + len("</section>")]


# --- default / status grouping ----------------------------------------------


async def test_default_group_is_status_kanban(cookie_client):
    """No group param -> the classic four status columns, drag-enabled."""
    r = await cookie_client.get("/")
    assert r.status_code == 200
    for status in ("draft", "queued", "running", "review"):
        section = _section_html(r.text, status)
        assert section, f"missing status column {status}"
        # Status columns keep status-changing drag.
        assert 'data-droppable="1"' in section
        assert f'data-status="{status}"' in section


async def test_group_by_selector_present(cookie_client):
    r = await cookie_client.get("/")
    assert "Group by" in r.text
    for g in ("status", "label", "project", "priority"):
        assert f'data-board-group="{g}"' in r.text


async def test_invalid_group_falls_back_to_status(cookie_client):
    r = await cookie_client.get("/?group=bogus")
    assert r.status_code == 200
    # Falls back to the status kanban.
    assert _section_html(r.text, "draft")
    assert 'data-board-group="status"' in r.text


# --- label grouping ----------------------------------------------------------


async def test_group_by_label_columns_and_counts(cookie_client, session, profile):
    bug = create_label(session, name="bug", color="#ff0000")
    feat = create_label(session, name="feature", color="#00ff00")
    t1 = create_ticket(session, title="has-bug", prompt="x",
                       profile_id=profile.id, status="draft", source_path="/tmp")
    t2 = create_ticket(session, title="no-label", prompt="x",
                       profile_id=profile.id, status="draft", source_path="/tmp")
    set_ticket_labels(session, t1.id, [bug.id])
    session.commit()

    r = await cookie_client.get("/?group=label")
    assert r.status_code == 200
    body = r.text

    bug_section = _section_html(body, f"label-{bug.id}")
    feat_section = _section_html(body, f"label-{feat.id}")
    none_section = _section_html(body, "label-none")
    assert bug_section and feat_section and none_section

    # The labelled ticket appears under its label, the unlabelled under Unlabeled.
    assert f'data-ticket-id="{t1.id}"' in bug_section
    assert f'data-ticket-id="{t2.id}"' in none_section
    assert f'data-ticket-id="{t1.id}"' not in none_section

    # Group counts: bug=1, feature=0 (empty-state), Unlabeled=1.
    assert "&middot; 1" in bug_section
    assert "&middot; 0" in feat_section
    assert "board-empty" in feat_section  # empty-state placeholder
    assert "Unlabeled" in none_section


async def test_label_grouping_is_read_only(cookie_client, session, profile):
    create_label(session, name="bug")
    r = await cookie_client.get("/?group=label")
    body = r.text
    # No property-grouping column is a status-drag target.
    none_section = _section_html(body, "label-none")
    assert 'data-droppable="0"' in none_section
    assert 'data-droppable="1"' not in body  # no status columns at all here


async def test_multi_label_ticket_appears_in_each_column(cookie_client, session, profile):
    bug = create_label(session, name="bug")
    feat = create_label(session, name="feature")
    t = create_ticket(session, title="multi", prompt="x",
                      profile_id=profile.id, status="draft", source_path="/tmp")
    set_ticket_labels(session, t.id, [bug.id, feat.id])
    session.commit()

    body = (await cookie_client.get("/?group=label")).text
    assert f'data-ticket-id="{t.id}"' in _section_html(body, f"label-{bug.id}")
    assert f'data-ticket-id="{t.id}"' in _section_html(body, f"label-{feat.id}")
    # ...but not under Unlabeled.
    assert f'data-ticket-id="{t.id}"' not in _section_html(body, "label-none")


# --- project grouping --------------------------------------------------------


async def test_group_by_project_columns(cookie_client, session, profile):
    proj = create_project(session, name="Alpha", slug="alpha", source_path="/tmp")
    t1 = create_ticket(session, title="in-proj", prompt="x", profile_id=profile.id,
                       status="draft", source_path="/tmp", project_id=proj.id)
    t2 = create_ticket(session, title="no-proj", prompt="x", profile_id=profile.id,
                       status="draft", source_path="/tmp")
    session.commit()

    body = (await cookie_client.get("/?group=project")).text
    proj_section = _section_html(body, f"project-{proj.id}")
    none_section = _section_html(body, "project-none")
    assert f'data-ticket-id="{t1.id}"' in proj_section
    assert f'data-ticket-id="{t2.id}"' in none_section
    assert "No project" in none_section
    assert 'data-droppable="0"' in proj_section


# --- priority grouping -------------------------------------------------------


async def test_group_by_priority_buckets_and_empty_state(cookie_client, session, profile):
    high = create_ticket(session, title="urgent", prompt="x", profile_id=profile.id,
                         status="draft", source_path="/tmp", priority=2)
    low = create_ticket(session, title="whenever", prompt="x", profile_id=profile.id,
                        status="draft", source_path="/tmp", priority=0)
    session.commit()

    body = (await cookie_client.get("/?group=priority")).text
    # The base 0..2 scale always renders, highest first, so the empty middle
    # bucket is visible with an empty-state.
    assert "High" in _section_html(body, "priority-2")
    assert "Medium" in _section_html(body, "priority-1")
    assert "Low" in _section_html(body, "priority-0")
    assert f'data-ticket-id="{high.id}"' in _section_html(body, "priority-2")
    assert f'data-ticket-id="{low.id}"' in _section_html(body, "priority-0")
    # Priority 1 has no tickets -> empty-state.
    assert "board-empty" in _section_html(body, "priority-1")
    assert 'data-droppable="0"' in _section_html(body, "priority-2")


# --- polled OOB fragment honours the grouping --------------------------------


async def test_columns_poll_matches_grouping(cookie_client, session, profile):
    bug = create_label(session, name="bug")
    t = create_ticket(session, title="x", prompt="x", profile_id=profile.id,
                      status="draft", source_path="/tmp")
    set_ticket_labels(session, t.id, [bug.id])
    session.commit()

    r = await cookie_client.get("/board/columns?group=label")
    assert r.status_code == 200
    body = r.text
    # OOB sections keyed by label, each opting into the out-of-band swap.
    assert f'id="board-col-label-{bug.id}"' in body
    assert 'id="board-col-label-none"' in body
    assert 'hx-swap-oob="true"' in body
    assert f'data-ticket-id="{t.id}"' in _section_html(body, f"label-{bug.id}")


async def test_columns_poll_defaults_to_status(cookie_client):
    r = await cookie_client.get("/board/columns")
    body = r.text
    for status in ("draft", "queued", "running", "review"):
        assert f'id="board-col-{status}"' in body


# --- grouping x query interaction --------------------------------------------


async def test_query_filters_grouped_board(cookie_client, session, profile):
    bug = create_label(session, name="bug")
    keep = create_ticket(session, title="keep-me", prompt="x", profile_id=profile.id,
                         status="draft", source_path="/tmp")
    drop = create_ticket(session, title="drop-me", prompt="x", profile_id=profile.id,
                         status="review", source_path="/tmp")
    set_ticket_labels(session, keep.id, [bug.id])
    set_ticket_labels(session, drop.id, [bug.id])
    session.commit()

    # Narrow to draft status while grouping by label: only the draft ticket
    # survives, still bucketed under its label.
    body = (await cookie_client.get("/?group=label&q=status%3Ddraft")).text
    bug_section = _section_html(body, f"label-{bug.id}")
    assert f'data-ticket-id="{keep.id}"' in bug_section
    assert f'data-ticket-id="{drop.id}"' not in body


async def test_label_query_with_label_grouping(cookie_client, session, profile):
    bug = create_label(session, name="bug")
    feat = create_label(session, name="feature")
    a = create_ticket(session, title="a", prompt="x", profile_id=profile.id,
                      status="draft", source_path="/tmp")
    b = create_ticket(session, title="b", prompt="x", profile_id=profile.id,
                      status="draft", source_path="/tmp")
    set_ticket_labels(session, a.id, [bug.id])
    set_ticket_labels(session, b.id, [feat.id])
    session.commit()

    # label=bug filter + group by label: only ticket a shows, in the bug column.
    body = (await cookie_client.get("/?group=label&q=label%3Dbug")).text
    assert f'data-ticket-id="{a.id}"' in _section_html(body, f"label-{bug.id}")
    assert f'data-ticket-id="{b.id}"' not in body


# --- empty board -------------------------------------------------------------


async def test_empty_board_grouped_shows_empty_states(cookie_client, session, profile):
    create_label(session, name="bug")
    session.commit()
    body = (await cookie_client.get("/?group=label")).text
    # The defined label column and the Unlabeled bucket both render empty-states.
    assert "board-empty" in _section_html(body, "label-none")
