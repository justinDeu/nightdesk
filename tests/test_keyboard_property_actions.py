"""Tests for the focused-ticket keyboard property action shortcuts.

Covers:
  - The command palette JS is present and wired on board + ticket detail pages.
  - The cheatsheet documents all focused-ticket shortcuts (E, P, S, L, A, R, Space).
  - The board cursor J/K shortcut is documented.
  - The property picker JS is present (ndOpenPropertyPicker entry point).
  - The cursor highlight CSS class (nd-cursor-active) is shipped.
  - The cookie-auth action routes used by shortcuts (run-now, archive, requeue)
    return the expected status codes.
  - The generic property routes used by P/S shortcuts work for focused updates.

These are server-side rendering + route integration tests. Full keyboard event
dispatch testing would require a browser automation framework (Playwright/
Selenium) and is left as a focused manual validation note below.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket

# The real static directory (siblings of this source tree).
_SRC_STATIC = Path(__file__).resolve().parent.parent / "src" / "nightdesk" / "static"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(engine, tmp_path):
    # Copy real static assets so /static/app.css etc. are served.
    static_dst = tmp_path / "static"
    if _SRC_STATIC.is_dir():
        shutil.copytree(str(_SRC_STATIC), str(static_dst), dirs_exist_ok=True)
    return create_app(
        engine=engine,
        bearer_token="t",
        static_root=static_dst,
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )


@pytest.fixture
async def cookie_client(app):
    """Cookie-auth client simulating browser sessions."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


@pytest.fixture
def _profile(session):
    return create_profile(
        session,
        name="kb-test",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


def _make(session, profile, **kw):
    """Create a ticket with defaults matching the test_property_picker pattern."""
    kw.setdefault("title", "kb-ticket")
    kw.setdefault("prompt", "")
    kw.setdefault("source_path", "/tmp")
    return create_ticket(session, profile_id=profile.id, **kw)


# ---------------------------------------------------------------------------
# Cheatsheet / markup tests
# ---------------------------------------------------------------------------


async def test_cheatsheet_includes_focused_ticket_section(cookie_client):
    """The cheat sheet must document all focused-ticket shortcuts."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    # Focused ticket section header
    assert "Focused ticket" in r.text
    # Individual shortcuts
    assert "Edit focused ticket" in r.text
    assert "Set priority" in r.text
    assert "Set status" in r.text
    assert "Set labels" in r.text
    assert "Archive" in r.text
    assert "Run now / Requeue" in r.text
    assert "Peek ticket" in r.text


async def test_cheatsheet_includes_board_navigation_section(cookie_client):
    """The cheat sheet must document J/K board cursor navigation."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "Board navigation" in r.text
    assert "Next / previous ticket" in r.text


async def test_command_palette_js_includes_cursor_css(cookie_client):
    """The cursor highlight CSS class must be in the stylesheet."""
    r = await cookie_client.get("/static/app.css")
    assert r.status_code == 200
    assert "nd-cursor-active" in r.text


async def test_property_picker_js_exposes_entry_point(cookie_client):
    """The property picker JS must expose ndOpenPropertyPicker for shortcuts."""
    r = await cookie_client.get("/static/property_picker.js")
    assert r.status_code == 200
    assert "ndOpenPropertyPicker" in r.text


async def test_command_palette_js_has_shortcut_keys(cookie_client):
    """The command palette JS must wire all focused-ticket shortcut keys."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # Key bindings for focused-ticket actions
    assert '"e"' in r.text or "'e'" in r.text  # Edit
    assert '"p"' in r.text or "'p'" in r.text  # Priority
    assert '"s"' in r.text or "'s'" in r.text  # Status
    assert '"l"' in r.text or "'l'" in r.text  # Labels
    assert '"a"' in r.text or "'a'" in r.text  # Archive
    assert '"r"' in r.text or "'r'" in r.text  # Run/Requeue
    assert '" "' in r.text  # Space (peek)
    # Cursor navigation
    assert '"j"' in r.text or "'j'" in r.text
    assert '"k"' in r.text or "'k'" in r.text


async def test_shortcuts_ignored_while_typing(cookie_client):
    """The command palette JS must skip shortcuts inside input fields."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "isTypingTarget" in r.text
    assert "input" in r.text
    assert "textarea" in r.text
    assert "isContentEditable" in r.text or "contentEditable" in r.text


async def test_picker_menu_open_defers_to_picker_js(cookie_client):
    """When a picker menu is open, global shortcuts must not hijack keys."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "data-property-menu" in r.text


# ---------------------------------------------------------------------------
# Route integration: cookie-auth action shortcuts
# ---------------------------------------------------------------------------


async def test_archive_shortcut_route(cookie_client, session, _profile):
    """A shortcut-triggered archive hits /tickets/{id}/archive."""
    t = _make(session, _profile, title="kb-archive-test")
    # Move through lifecycle to review so archive is valid.
    from nightdesk.domain.tickets import transition_status, request_run_now
    transition_status(session, t.id, "queued")
    request_run_now(session, t.id)
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")

    r = await cookie_client.post(
        f"/tickets/{t.id}/archive",
        headers={"HX-Request": "true"},
    )
    assert r.status_code in (200, 204)


async def test_run_now_shortcut_route(cookie_client, session, _profile):
    """A shortcut-triggered run-now hits /tickets/{id}/run-now."""
    t = _make(session, _profile, title="kb-run-test")

    r = await cookie_client.post(
        f"/tickets/{t.id}/run-now",
        headers={"HX-Request": "true"},
    )
    assert r.status_code in (200, 204)


async def test_requeue_shortcut_route(cookie_client, session, _profile):
    """A shortcut-triggered requeue hits /tickets/{id}/requeue."""
    t = _make(session, _profile, title="kb-requeue-test")
    from nightdesk.domain.tickets import transition_status, request_run_now
    transition_status(session, t.id, "queued")
    request_run_now(session, t.id)
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")

    r = await cookie_client.post(
        f"/tickets/{t.id}/requeue",
        headers={"HX-Request": "true"},
    )
    # Requeue returns 303 (redirect) on success when not an HTMX swap target.
    assert r.status_code in (200, 204, 303)
# ---------------------------------------------------------------------------


async def test_priority_picker_route_for_shortcut(cookie_client, session, _profile):
    """P shortcut opens the priority picker via /board/tickets/{id}/picker/priority."""
    t = _make(session, _profile, title="kb-priority-test")

    r = await cookie_client.get(f"/board/tickets/{t.id}/picker/priority")
    assert r.status_code == 200
    assert "priority" in r.text.lower() or "Priority" in r.text


async def test_status_picker_route_for_shortcut(cookie_client, session, _profile):
    """S shortcut opens the status picker via /board/tickets/{id}/picker/status."""
    t = _make(session, _profile, title="kb-status-test")

    r = await cookie_client.get(f"/board/tickets/{t.id}/picker/status")
    assert r.status_code == 200
    assert "status" in r.text.lower() or "Status" in r.text


async def test_property_update_route_for_priority_shortcut(cookie_client, session, _profile):
    """P shortcut commits via POST /board/tickets/{id}/property/priority."""
    t = _make(session, _profile, title="kb-prop-priority")

    r = await cookie_client.post(
        f"/board/tickets/{t.id}/property/priority",
        data={"value": "high"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200


async def test_property_update_route_for_status_shortcut(cookie_client, session, _profile):
    """S shortcut commits via POST /board/tickets/{id}/property/status."""
    t = _make(session, _profile, title="kb-prop-status")

    r = await cookie_client.post(
        f"/board/tickets/{t.id}/property/status",
        data={"value": "queued"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Board page: shortcut infrastructure present
# ---------------------------------------------------------------------------


async def test_board_has_property_picker_chips(cookie_client, session, _profile):
    """Board sidebar must render property picker chips for the selected ticket."""
    t = _make(session, _profile, title="kb-board-chips")

    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    # Property picker chips for priority and status in the sidebar
    assert f'data-property-chip="{t.id}:status"' in r.text
    assert f'data-property-chip="{t.id}:priority"' in r.text


async def test_board_cursor_target_attribute(cookie_client, session, _profile):
    """Board cards carry data-ticket-id for cursor targeting."""
    t = _make(session, _profile, title="kb-cursor-target")

    r = await cookie_client.get("/")
    assert r.status_code == 200
    assert f'data-ticket-id="{t.id}"' in r.text


# ---------------------------------------------------------------------------
# Manual validation notes
# ---------------------------------------------------------------------------

# The following behaviors require browser-based manual validation or a
# Playwright/Selenium test suite:
#
# 1. J/K moves the cursor between cards and opens the sidebar for each.
# 2. P opens the priority picker popover on the sidebar.
# 3. S opens the status picker popover on the sidebar.
# 4. L flashes "labels not yet configured" (until labels model lands).
# 5. E opens the ticket edit modal.
# 6. A archives the focused ticket (valid from review status).
# 7. R runs-now (draft/queued) or requeues (review/archived).
# 8. Space flashes the peek placeholder.
# 9. All shortcuts are suppressed when typing in inputs/textareas.
# 10. Shortcuts are suppressed when a foreign dialog or picker menu is open.
# 11. Cursor highlight (nd-cursor-active) persists across 3s column polls.
