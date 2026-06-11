"""Cross-cutting integration tests for the management UX surfaces.

This module exercises end-to-end representative flows that span multiple UX
features in a single test. Each test follows a realistic user journey:

  1. Label a ticket, then filter and group by that label.
  2. Keyboard cursor attributes survive across board operations.
  3. Inbox promote: capture → flesh out → promote to board → verify.
  4. Display settings via query params (the URL-backed display contract).
  5. Execution preview provenance across project/profile/ticket layers.

These complement the per-feature suites (test_labels, test_board_grouping,
test_list_view, test_inbox_promote, test_effective_config, etc.) by testing
the seams between features in a single coherent flow.
"""
import re

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.domain.labels import create_label, set_ticket_labels
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.tickets import create_ticket


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
        transport=transport, base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


@pytest.fixture
def profile(session):
    return create_profile(
        session, name="integ-mgmt", fs_read=[], fs_write=[],
        allowed_tools=[], denied_tools=[], network_mode="off",
        network_allowlist=[], secret_keys=[], default_model=None,
    )


def _mk(session, profile, title, *, status="draft", **kw):
    return create_ticket(
        session, title=title, prompt="integ", profile_id=profile.id,
        status=status, source_path="/tmp", **kw,
    )


def _row_ids(text):
    """Ticket ids rendered as real board cards / list rows."""
    return set(re.findall(r'data-ticket-id="([^"]+)"', text))


def _inbox(session, profile, *, title="inbox item", complete=True, **kw):
    """Create an inbox ticket. If complete=True, add a source_path so it is
    promotable. If False, leave it under-specified."""
    create_kw = dict(
        title=title,
        prompt="inbox integ test",
        status="inbox",
        profile_id=profile.id,
    )
    if complete:
        create_kw["source_path"] = "/tmp"
    create_kw.update(kw)
    return create_ticket(session, **create_kw)


# ======================================================================== #
# Flow 1: Label a ticket, filter/group by label                            #
# ======================================================================== #


@pytest.mark.anyio
async def test_label_then_filter_then_group(cookie_client, session, profile):
    """End-to-end: create a label, assign it to some tickets, verify that:
    1) a query filter `label:bug` returns only those tickets,
    2) the board grouped by label puts them in a dedicated column."""
    # Create labels
    bug = create_label(session, name="bug", color="#ef4444")
    feat = create_label(session, name="feature", color="#06b6d4")

    # Create tickets
    t1 = _mk(session, profile, "Fix null pointer in parser")
    t2 = _mk(session, profile, "Add pagination to list view")
    t3 = _mk(session, profile, "Refactor middleware stack")

    set_ticket_labels(session, t1.id, [bug.id])
    set_ticket_labels(session, t2.id, [feat.id])
    # t3 has no labels

    # 1) Filter by label:bug on the board
    r = await cookie_client.get("/", params={"q": "label:bug"})
    assert r.status_code == 200
    board_ids = _row_ids(r.text)
    assert t1.id in board_ids, "bug-labelled ticket missing from filtered board"
    assert t2.id not in board_ids, "non-bug ticket appeared in label:bug filter"
    assert t3.id not in board_ids, "unlabelled ticket appeared in label:bug filter"

    # 2) Group by label on the board
    r = await cookie_client.get("/", params={"group": "label"})
    assert r.status_code == 200
    text = r.text
    # All three tickets should appear somewhere (grouped board shows all)
    all_ids = _row_ids(text)
    assert t1.id in all_ids
    assert t2.id in all_ids
    assert t3.id in all_ids
    # The label column header should contain the label name
    assert "bug" in text.lower(), "label column header missing 'bug'"
    assert "feature" in text.lower(), "label column header missing 'feature'"


@pytest.mark.anyio
async def test_bulk_label_then_list_shows_labels(cookie_client, session, profile):
    """Bulk-add labels via the inline label picker's add-label route,
    then verify the list view displays label chips for the updated tickets."""
    lbl = create_label(session, name="cleanup", color="#6b7280")

    t1 = _mk(session, profile, "Remove deprecated API")
    t2 = _mk(session, profile, "Delete unused imports")

    # Bulk add labels
    r = await cookie_client.post(
        "/board/tickets/bulk/labels",
        data={"ticket_ids": f"{t1.id},{t2.id}", "label_ids": lbl.id},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200

    # Verify list view shows label chips
    r = await cookie_client.get("/list")
    assert r.status_code == 200
    text = r.text
    # Both tickets should appear in the list
    assert t1.id in _row_ids(text)
    assert t2.id in _row_ids(text)
    # The label name should appear in the rendered rows
    assert "cleanup" in text


# ======================================================================== #
# Flow 2: Keyboard cursor navigation attributes                            #
# ======================================================================== #


@pytest.mark.anyio
async def test_board_cards_carry_cursor_targets(cookie_client, session, profile):
    """Board cards carry data-ticket-id so the H/J/K/L keyboard spine can
    identify and highlight them. The cursor JS is loaded from the command
    palette script."""
    t1 = _mk(session, profile, "Cursor target A")
    t2 = _mk(session, profile, "Cursor target B")
    session.commit()

    r = await cookie_client.get("/")
    assert r.status_code == 200
    text = r.text

    # Both tickets should have data-ticket-id (the cursor target)
    assert f'data-ticket-id="{t1.id}"' in text
    assert f'data-ticket-id="{t2.id}"' in text

    # The command_palette.js (which houses the cursor spine) is loaded
    assert "command_palette.js" in text


@pytest.mark.anyio
async def test_list_rows_carry_cursor_ids(cookie_client, session, profile):
    """List rows carry data-ticket-id for the keyboard cursor."""
    t1 = _mk(session, profile, "List cursor A")
    _mk(session, profile, "List cursor B")
    session.commit()

    r = await cookie_client.get("/list")
    assert r.status_code == 200
    text = r.text

    assert f'data-ticket-id="{t1.id}"' in text


@pytest.mark.anyio
async def test_cursor_survives_board_polling(cookie_client, session, profile):
    """The polled OOB fragment preserves data-ticket-id so the JS cursor
    restoration logic (restoreCursor on htmx:oobAfterSwap) can re-attach
    after a poll cycle."""
    t1 = _mk(session, profile, "Poll survive")
    session.commit()

    # Simulate a poll (HTMX OOB swap request)
    r = await cookie_client.get("/board/columns", headers={"HX-Request": "true"})
    assert r.status_code == 200
    text = r.text
    assert f'data-ticket-id="{t1.id}"' in text


# ======================================================================== #
# Flow 3: Inbox promote                                                     #
# ======================================================================== #


@pytest.mark.anyio
async def test_inbox_capture_promote_verify(cookie_client, session, profile):
    """Full inbox flow: capture an item → verify it's in inbox → open promote
    modal → promote to draft → verify it appears on the board."""
    # Capture an incomplete item (no workspace)
    t = _inbox(session, profile, title="Capture then promote", complete=False)
    session.commit()

    # Verify it appears in the inbox
    r = await cookie_client.get("/inbox")
    assert r.status_code == 200
    assert t.id in r.text
    assert "capture then promote" in r.text.lower()

    # The promote action should be present
    assert "promote" in r.text.lower()

    # Promote to draft via full form (fill in workspace)
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data={
            "target": "draft",
            "title": "Capture then promote",
            "prompt": "Filled in during promotion",
            "profile_id": profile.id,
            "workspace_form": "1",
            "source_path": "/tmp",
            "priority": "2",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200

    # Verify the ticket is now a draft (on the board)
    session.expire_all()
    r = await cookie_client.get("/")
    assert r.status_code == 200
    board_ids = _row_ids(r.text)
    assert t.id in board_ids, "Promoted ticket not found on the board"


@pytest.mark.anyio
async def test_inbox_promote_preserves_labels(cookie_client, session, profile):
    """Labels assigned to an inbox item survive promotion."""
    lbl = create_label(session, name="ui/ux", color="#8b5cf6")

    t = _inbox(session, profile, title="Labelled inbox item", complete=True)
    set_ticket_labels(session, t.id, [lbl.id])
    session.commit()

    # Promote to draft
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data={
            "target": "draft",
            "title": "Labelled inbox item",
            "prompt": "test",
            "profile_id": profile.id,
            "workspace_form": "1",
            "source_path": "/tmp",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200

    # Verify on the board that the label chip is rendered
    r = await cookie_client.get("/")
    assert r.status_code == 200
    text = r.text
    assert t.id in _row_ids(text)
    assert "ui/ux" in text


@pytest.mark.anyio
async def test_inbox_queue_blocked_for_incomplete(cookie_client, session, profile):
    """Queueing an incomplete inbox item (no workspace) is rejected with 422."""
    t = _inbox(session, profile, title="Incomplete", complete=False)
    session.commit()

    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data={
            "target": "queued",
            "title": "Incomplete",
            "prompt": "test",
            "profile_id": profile.id,
            # No source_path / workspace_form
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 422


# ======================================================================== #
# Flow 4: Display settings via query params (URL-backed display)            #
# ======================================================================== #


@pytest.mark.anyio
async def test_display_group_order_via_query_params(cookie_client, session, profile):
    """Display settings (group + order) are carried by query params, not
    persisted state. Cycling through options changes arrangement but not
    membership."""
    for i in range(6):
        _mk(session, profile, f"display-{i}", priority=i % 4)
    session.commit()

    baseline = None
    for group in ("status", "priority", "none"):
        for order in ("manual", "priority", "title"):
            r = await cookie_client.get(f"/list?group={group}&order={order}")
            assert r.status_code == 200
            members = _row_ids(r.text)
            if baseline is None:
                baseline = members
            else:
                assert members == baseline, (
                    f"group={group}&order={order} changed membership"
                )


@pytest.mark.anyio
async def test_display_group_renders_headers(cookie_client, session, profile):
    """Grouped list view renders group headers with counts."""
    _mk(session, profile, "High prio", priority=4)
    _mk(session, profile, "Low prio", priority=0)
    session.commit()

    r = await cookie_client.get("/list?group=priority")
    assert r.status_code == 200
    text = r.text

    # Group headers should contain priority labels (at minimum "Urgent" for 4
    # and "None" for 0, matching the PRIORITY_SCALE)
    from nightdesk.domain.priority import priority_label
    assert priority_label(4) in text, f"Missing priority-4 header ({priority_label(4)})"
    assert priority_label(0) in text, f"Missing priority-0 header ({priority_label(0)})"


@pytest.mark.anyio
async def test_board_group_param_changes_layout(cookie_client, session, profile):
    """The board's group query param changes the column layout from status
    to the requested grouping dimension."""
    _mk(session, profile, "Grouped board A")
    _mk(session, profile, "Grouped board B")
    session.commit()

    # Default (status grouping)
    r_default = await cookie_client.get("/")
    assert r_default.status_code == 200

    # Label grouping
    r_label = await cookie_client.get("/", params={"group": "label"})
    assert r_label.status_code == 200

    # Both should contain the tickets
    default_ids = _row_ids(r_default.text)
    label_ids = _row_ids(r_label.text)
    assert default_ids == label_ids, "Grouping changed board membership"


# ======================================================================== #
# Flow 5: Execution preview provenance                                      #
# ======================================================================== #


@pytest.mark.anyio
async def test_ticket_detail_shows_execution_preview(cookie_client, session, profile):
    """The ticket detail page includes an execution context preview section
    with provenance chips."""
    t = _mk(session, profile, "Preview provenance test")
    session.commit()

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    text = r.text

    # The detail page should mount the effective-config preview
    assert "data-effective-preview" in text or "effective-config" in text


@pytest.mark.anyio
async def test_execution_preview_with_project_provenance(
    cookie_client, session, profile
):
    """When a ticket belongs to a project with defaults, the execution
    preview HTML partial shows provenance chips."""
    project = create_project(
        session,
        name="Provenance project",
        slug="prov-proj",
        source_path="/tmp",
    )
    t = _mk(session, profile, "Project provenance", project_id=project.id)
    session.commit()

    # Fetch the effective config HTML partial (cookie-auth route)
    r = await cookie_client.get(f"/tickets/{t.id}/effective-config")
    assert r.status_code == 200
    text = r.text

    # The partial should contain provenance data
    assert "data-effective-config" in text or "provenance" in text.lower()


@pytest.mark.anyio
async def test_edit_modal_includes_live_preview(cookie_client, session, profile):
    """The ticket detail page renders the edit modal inline with a live-updating
    execution context preview that posts the form fields to the preview endpoint."""
    t = _mk(session, profile, "Edit modal preview")
    session.commit()

    # The edit modal is rendered inline on the ticket detail page
    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    text = r.text

    # The edit modal should contain the preview section
    assert "data-effective-preview-section" in text
    # The preview should post to the effective-config preview endpoint
    assert "effective-config" in text


# ======================================================================== #
# Cross-cutting: label + display + inbox in a single journey                #
# ======================================================================== #


@pytest.mark.anyio
async def test_full_journey_label_filter_group_promote(cookie_client, session, profile):
    """A complete user journey exercising multiple surfaces:
    1. Create labels and an inbox item with labels.
    2. Verify inbox shows the labelled item.
    3. Promote to queued.
    4. Filter the board by that label.
    5. Group the board by label and verify the ticket appears in the right group.
    """
    bug = create_label(session, name="bug", color="#ef4444")
    infra = create_label(session, name="infra", color="#dc2626")

    # Step 1: Create an inbox item with labels
    t = _inbox(session, profile, title="Journey: fix flaky CI test", complete=True)
    set_ticket_labels(session, t.id, [bug.id, infra.id])
    session.commit()

    # Step 2: Verify inbox
    r = await cookie_client.get("/inbox")
    assert r.status_code == 200
    assert t.id in r.text

    # Step 3: Promote to queued
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data={
            "target": "queued",
            "title": "Journey: fix flaky CI test",
            "prompt": "Fix the flaky integration test",
            "profile_id": profile.id,
            "workspace_form": "1",
            "source_path": "/tmp",
            "priority": "3",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200

    # Step 4: Filter board by label:bug
    r = await cookie_client.get("/", params={"q": "label:bug"})
    assert r.status_code == 200
    board_filtered = _row_ids(r.text)
    assert t.id in board_filtered, "Promoted+labelled ticket not in label:bug filter"

    # Step 5: Group board by label
    r = await cookie_client.get("/", params={"group": "label"})
    assert r.status_code == 200
    grouped_text = r.text
    grouped_ids = _row_ids(grouped_text)
    assert t.id in grouped_ids, "Ticket missing from grouped board"
    assert "bug" in grouped_text.lower()
    assert "infra" in grouped_text.lower()

    # Also verify the list view shows it
    r = await cookie_client.get("/list")
    assert r.status_code == 200
    assert t.id in _row_ids(r.text)
