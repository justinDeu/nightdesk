"""Cross-feature integration tests for the Views / List / Bulk merge point.

The per-feature suites (``test_board_grouping``, ``test_list_view``,
``test_list_inline_edits``, ``test_bulk_actions``) each cover one branch in
isolation. This module exercises the *seams between them* — the behaviour the
``integration/ux-views-list-bulk`` ticket exists to guarantee:

  * Filter membership (the query) and display arrangement (group/order) are
    separate concerns: changing the display never changes *which* tickets are
    shown, and board ↔ list always agree on membership for the same query.
  * Project-scoped views work via the ``project`` filter alone — no dedicated
    project dashboard surface is required, and display settings still apply
    inside a project scope.
  * Board/list toggle + group-by-label: the board groups by label (the
    "research separation" lens) while the list groups by its own display keys,
    both over the identical filtered set.
  * Bulk actions mutate the shared ticket set, and the mutation is reflected in
    both the board and the list (including list grouping/ordering that keys off
    the mutated property).

Note (integration risk, asserted indirectly): there is no Saved Views surface
in this branch — ``ux/saved-views-model`` / ``ux/saved-views-ui`` /
``ux/display-model`` merged as no-ops (identical to ``main``). The display
model that the list consumes lives in ``nightdesk.domain.display`` and is
URL/query-backed rather than persisted. These tests pin the display-via-query
contract that any future Saved Views feature would build on.
"""
import re

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.domain.labels import create_label, set_ticket_labels
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
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
        session, name="integ-test", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


def _mk(session, profile, title, status="draft", **kw):
    return create_ticket(
        session, title=title, prompt="x", profile_id=profile.id,
        status=status, source_path="/tmp", **kw,
    )


def _row_ids(text):
    """Ticket ids rendered as real board cards / list rows (``data-ticket-id``).

    Ignores ticket data the create-modal dependency picker embeds for every
    ticket, exactly like the per-feature suites' helper.
    """
    return set(re.findall(r'data-ticket-id="([^"]+)"', text))


# --------------------------------------------------------------------------- #
# Filter membership vs. display arrangement are separate concerns
# --------------------------------------------------------------------------- #


async def test_board_and_list_agree_on_membership_regardless_of_display(
    cookie_client, session, profile
):
    """Board and list show the *same* ticket set for the same query, even when
    the list is grouped/ordered differently from the board."""
    ids = {_mk(session, profile, f"t{i}", priority=i % 3).id for i in range(6)}
    session.commit()

    board = _row_ids((await cookie_client.get("/")).text)
    # List with a non-default display arrangement.
    list_grouped = _row_ids(
        (await cookie_client.get("/list?group=priority&order=title")).text
    )

    assert ids <= board, "board missing seeded tickets"
    assert board == list_grouped, "display arrangement changed membership"


async def test_display_changes_never_change_membership(cookie_client, session, profile):
    """Cycling through every grouping/ordering on the list keeps membership
    constant — display is a pure rearrangement of a fixed filtered set."""
    ids = {_mk(session, profile, f"d{i}", priority=i).id for i in range(5)}
    session.commit()

    baseline = None
    for group in ("status", "project", "priority", "label", "profile", "none"):
        for order in ("manual", "priority", "updated", "created", "title"):
            body = (
                await cookie_client.get(f"/list?group={group}&order={order}")
            ).text
            members = _row_ids(body)
            assert ids <= members, f"{group}/{order} dropped tickets"
            if baseline is None:
                baseline = members
            else:
                assert members == baseline, (
                    f"{group}/{order} changed membership"
                )


async def test_query_filter_applies_identically_to_board_and_list(
    cookie_client, session, profile
):
    """A structured query narrows membership the same way on both surfaces, and
    the list's display grouping does not widen it back."""
    keep = _mk(session, profile, "only-draft", "draft").id
    drop = _mk(session, profile, "only-review", "review").id
    session.commit()

    # ``status=draft`` is a structured filter (no FTS connection), matching the
    # per-feature suites; the point here is board ↔ list parity under display.
    board = _row_ids((await cookie_client.get("/?q=status%3Ddraft")).text)
    listed = _row_ids(
        (await cookie_client.get("/list?q=status%3Ddraft&group=priority")).text
    )

    assert keep in board and drop not in board
    assert board == listed


# --------------------------------------------------------------------------- #
# Project-scoped views (no dedicated project dashboard)
# --------------------------------------------------------------------------- #


async def test_project_scoped_view_on_board_and_list(cookie_client, session, profile):
    """The ``project`` filter scopes both surfaces to one project's tickets,
    and the list's display settings still apply within that scope."""
    proj = create_project(session, name="Apollo", slug="apollo", source_path="/tmp/apollo")
    other = create_project(session, name="Gemini", slug="gemini", source_path="/tmp/gemini")
    in_scope = {_mk(session, profile, f"a{i}", project_id=proj.id).id for i in range(3)}
    out_scope = {_mk(session, profile, f"g{i}", project_id=other.id).id for i in range(2)}
    session.commit()

    # ``?project=<slug>`` is the project-scoped view — no dedicated dashboard.
    board = _row_ids((await cookie_client.get(f"/?project={proj.slug}")).text)
    listed = _row_ids(
        (await cookie_client.get(f"/list?project={proj.slug}&group=priority&order=title")).text
    )

    assert in_scope <= board
    assert not (out_scope & board), "board leaked other-project tickets"
    assert board == listed, "project-scoped list disagreed with board membership"


async def test_project_scope_combines_with_query(cookie_client, session, profile):
    proj = create_project(session, name="Apollo", slug="apollo", source_path="/tmp/apollo")
    hit = _mk(session, profile, "rocket-engine", status="draft", project_id=proj.id).id
    miss_proj = _mk(session, profile, "rocket-engine", status="draft", project_id=None).id
    miss_status = _mk(session, profile, "rocket-engine", status="review", project_id=proj.id).id
    session.commit()

    # Compose project scope with a structured filter (status) in the same query.
    listed = _row_ids(
        (await cookie_client.get(f"/list?q=project%3Dapollo%20status%3Ddraft")).text
    )
    assert hit in listed
    assert miss_proj not in listed    # wrong project
    assert miss_status not in listed  # wrong status


# --------------------------------------------------------------------------- #
# Board/list toggle + group-by-label (research-separation lens)
# --------------------------------------------------------------------------- #


async def test_group_by_label_board_vs_list_membership(cookie_client, session, profile):
    """Group-by-label is the board's research-separation lens; the list groups
    by its own display keys. Both render the identical filtered membership —
    labels arrange the board without filtering the underlying set."""
    bug = create_label(session, name="bug", color="#ff0000")
    feat = create_label(session, name="feature", color="#00ff00")
    t_bug = _mk(session, profile, "has-bug")
    t_multi = _mk(session, profile, "bug-and-feature")
    t_none = _mk(session, profile, "unlabeled")
    set_ticket_labels(session, t_bug.id, [bug.id])
    set_ticket_labels(session, t_multi.id, [bug.id, feat.id])
    session.commit()

    board = (await cookie_client.get("/?group=label")).text
    listed = (await cookie_client.get("/list?group=status")).text

    # Membership parity across the toggle.
    assert _row_ids(board) == _row_ids(listed)

    # The board separates by label without dropping anyone: a multi-label ticket
    # shows under each of its labels, an unlabeled one under "none".
    bug_col = re.search(
        rf'id="board-col-label-{bug.id}".*?(?=id="board-col-|\Z)', board, re.S
    )
    none_col = re.search(
        r'id="board-col-label-none".*?(?=id="board-col-|\Z)', board, re.S
    )
    assert bug_col and none_col
    assert t_bug.id in _row_ids(bug_col.group(0))
    assert t_multi.id in _row_ids(bug_col.group(0))
    assert t_none.id in _row_ids(none_col.group(0))
    # Membership (filter) is unchanged: the labelled tickets are still in the
    # list view even though it doesn't group by label.
    assert {t_bug.id, t_multi.id, t_none.id} <= _row_ids(listed)


# --------------------------------------------------------------------------- #
# Bulk actions mutate the shared set and both surfaces reflect it
# --------------------------------------------------------------------------- #


async def test_bulk_priority_change_reflects_in_list_grouping(
    cookie_client, session, profile
):
    """A bulk priority update is reflected when the list is grouped by priority:
    the bulk-actions branch mutates the same set the display branch renders."""
    a = _mk(session, profile, "a", priority=0)
    b = _mk(session, profile, "b", priority=0)
    session.commit()

    resp = await cookie_client.post(
        "/board/tickets/bulk/priority",
        data={"ticket_ids": f"{a.id},{b.id}", "priority": "2"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert {u["id"] for u in payload["updated"]} == {a.id, b.id}

    # List grouped by priority now buckets both tickets under priority:2.
    body = (await cookie_client.get("/list?group=priority&order=manual")).text
    bucket = re.search(
        r'data-column="priority:2".*?(?=data-column="|\Z)', body, re.S
    )
    assert bucket, "no priority:2 group rendered after bulk update"
    assert {a.id, b.id} <= _row_ids(bucket.group(0))


async def test_bulk_project_assignment_scopes_subsequent_views(
    cookie_client, session, profile
):
    """Bulk-assigning a project makes those tickets appear in the project-scoped
    view — bulk actions and project-scoped views compose."""
    proj = create_project(session, name="Apollo", slug="apollo", source_path="/tmp/apollo")
    a = _mk(session, profile, "a")
    b = _mk(session, profile, "b")
    c = _mk(session, profile, "c")  # left unassigned
    session.commit()

    # Bulk route assigns by project *id*; the scoped view filters by *slug*.
    resp = await cookie_client.post(
        "/board/tickets/bulk/project",
        data={"ticket_ids": f"{a.id},{b.id}", "project_id": proj.id},
    )
    assert resp.status_code == 200

    scoped = _row_ids((await cookie_client.get(f"/list?project={proj.slug}")).text)
    assert {a.id, b.id} <= scoped
    assert c.id not in scoped


# --------------------------------------------------------------------------- #
# List group-by-label: membership, multi-label, Unlabeled
# --------------------------------------------------------------------------- #


async def test_list_group_by_label_membership_and_multi_label(
    cookie_client, session, profile
):
    """The list group-by-label arranges tickets without changing membership.

    A multi-label ticket appears in each label group; unlabeled tickets land
    in the Unlabeled bucket; the full membership equals the non-grouped list.
    """
    bug = create_label(session, name="bug", color="#ff0000")
    feat = create_label(session, name="feature", color="#00ff00")
    t_bug = _mk(session, profile, "only-bug")
    t_multi = _mk(session, profile, "bug-and-feature")
    t_none = _mk(session, profile, "no-label")
    set_ticket_labels(session, t_bug.id, [bug.id])
    set_ticket_labels(session, t_multi.id, [bug.id, feat.id])
    session.commit()

    body = (await cookie_client.get("/list?group=label")).text

    # All three tickets appear somewhere.
    assert t_bug.id in body
    assert t_multi.id in body
    assert t_none.id in body

    # t_multi has two labels so its id appears at least twice.
    assert body.count(f'data-ticket-id="{t_multi.id}"') >= 2

    # The unlabeled ticket lands in the Unlabeled group.
    assert "Unlabeled" in body

    # Group label headers appear.
    assert "bug" in body
    assert "feature" in body

    # Membership is identical to the flat (group=none) list.
    flat = _row_ids((await cookie_client.get("/list?group=none")).text)
    labeled = _row_ids(body)
    assert flat == labeled


async def test_bulk_profile_change_reflects_in_list(
    cookie_client, session, profile
):
    """A bulk profile update via POST /board/tickets/bulk/profile is reflected
    in the list view — bulk mutations and list display compose correctly."""
    from nightdesk.domain.profiles import create_profile as _cp
    other = _cp(
        session, name="other-prof", fs_read=[], fs_write=[],
        allowed_tools=[], denied_tools=[], network_mode="off",
        network_allowlist=[], secret_keys=[], default_model=None,
    )
    a = _mk(session, profile, "prof-change-a")
    b = _mk(session, profile, "prof-change-b")
    session.commit()

    resp = await cookie_client.post(
        "/board/tickets/bulk/profile",
        data={"ticket_ids": f"{a.id},{b.id}", "profile_id": other.id},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert {u["id"] for u in payload["updated"]} == {a.id, b.id}

    # Both tickets now render under the new profile in the list grouped by profile.
    body = (await cookie_client.get("/list?group=profile")).text
    assert {a.id, b.id} <= _row_ids(body)
    assert "other-prof" in body


# ---------------------------------------------------------------------------
# Display options: props is pure presentation, never membership
# ---------------------------------------------------------------------------


async def test_props_does_not_change_ticket_membership(
    cookie_client, session, profile
):
    """?props= only hides columns; board and list still agree on membership."""
    t = _mk(session, profile, "props-membership-int")
    session.commit()

    list_all = (await cookie_client.get("/list")).text
    list_min = (await cookie_client.get("/list?props=priority")).text
    board_min = (await cookie_client.get("/?props=priority")).text
    assert t.id in _row_ids(list_all)
    assert t.id in _row_ids(list_min)
    assert t.id in _row_ids(board_min)
