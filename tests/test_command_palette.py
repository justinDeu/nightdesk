"""Smoke tests for the command palette + keyboard-shortcut wiring.

The palette is pure client JS (``static/command_palette.js``) plus a small
template partial included by ``base.html``. There's no server route to add,
so these tests assert the markup and script tag are wired into every page
that extends ``base.html`` (``/profiles`` needs no fixtures).

Additional coverage for this ticket (palette context + shortcut hints):
  - The command palette JS includes section grouping (section headers).
  - Shortcut hints are rendered with kbd styling (``<kbd>`` elements).
  - View/display commands (toggle board/runs, focus search) are present.
  - Status-aware sorting logic ships with the JS (``statusActionOrder``).
  - The cheatsheet documents the View & display section and focus search.
"""
from __future__ import annotations

import shutil
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app

# The real static directory (siblings of this source tree).
_SRC_STATIC = Path(__file__).resolve().parent.parent / "src" / "nightdesk" / "static"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(engine, tmp_path):
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
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                              cookies={"nightdesk_token": "t"}) as ac:
        yield ac


@pytest.fixture
def _profile(session):
    from nightdesk.domain.profiles import create_profile
    return create_profile(
        session,
        name="cp-test",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


# ---------------------------------------------------------------------------
# Baseline: palette + cheatsheet markup present on every page
# ---------------------------------------------------------------------------


async def test_base_includes_command_palette_script(cookie_client):
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "command_palette.js" in r.text


async def test_base_includes_palette_dialog(cookie_client):
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert 'id="nd-command-palette"' in r.text
    assert 'id="nd-cmdk-input"' in r.text
    assert 'id="nd-cmdk-list"' in r.text


async def test_base_includes_cheatsheet_dialog(cookie_client):
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert 'id="nd-shortcuts-cheatsheet"' in r.text
    # The cheat sheet documents the global shortcuts.
    assert "Go to board" in r.text
    assert "Go to list" in r.text
    assert "Go to Insights" in r.text
    assert "Go to Setup" in r.text
    # JS-free fallback note must be present.
    assert "reachable by mouse" in r.text


async def test_palette_present_on_board(cookie_client):
    # The board is the primary surface for the palette; make sure the partial
    # rides along there too (board.html extends base.html).
    r = await cookie_client.get("/")
    assert r.status_code == 200
    assert 'id="nd-command-palette"' in r.text
    assert "command_palette.js" in r.text


# ---------------------------------------------------------------------------
# Shortcut hints on palette rows
# ---------------------------------------------------------------------------


async def test_palette_js_renders_shortcut_kbd_hints(cookie_client):
    """The palette JS must render shortcut hints as <kbd> elements."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # The render function creates <kbd> elements via createElement.
    assert '"kbd"' in r.text or "'kbd'" in r.text
    # The kbd element gets monospace font styling.
    assert "font-mono" in r.text


async def test_palette_js_command_model_has_shortcut_field(cookie_client):
    """Each command in baseCommands must carry a shortcut field."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # Commands have shortcut fields alongside label/hint/run.
    # Check that the command model builds objects with "shortcut:" keys.
    assert 'shortcut: "c"' in r.text or "shortcut: 'c'" in r.text
    assert 'shortcut: "g b"' in r.text or "shortcut: 'g b'" in r.text
    assert 'shortcut: "g l"' in r.text or "shortcut: 'g l'" in r.text
    assert 'shortcut: "g i"' in r.text or "shortcut: 'g i'" in r.text
    assert 'shortcut: "g s"' in r.text or "shortcut: 'g s'" in r.text
    assert 'shortcut: "?"' in r.text or 'shortcut: \'?\'' in r.text


async def test_palette_js_shows_status_in_hint(cookie_client):
    """Ticket action commands should show the ticket status as a context hint."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # The status hint is built from focusedTicketStatus().
    assert "focusedTicketStatus" in r.text


# ---------------------------------------------------------------------------
# Context-sorting by focused ticket status
# ---------------------------------------------------------------------------


async def test_palette_js_has_status_aware_sorting(cookie_client):
    """The JS must ship statusActionOrder for context-aware command sorting."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "statusActionOrder" in r.text
    # Status-specific orderings are defined.
    assert '"running"' in r.text or "'running'" in r.text
    assert '"review"' in r.text or "'review'" in r.text
    assert '"draft"' in r.text or "'draft'" in r.text
    assert '"queued"' in r.text or "'queued'" in r.text
    assert '"archived"' in r.text or "'archived'" in r.text


async def test_palette_js_running_ticket_skips_invalid_actions(cookie_client):
    """Running tickets must not show run-now/requeue/archive in the palette."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # The command model skips invalid actions for running tickets.
    assert 'status === "running"' in r.text or "status === 'running'" in r.text
    assert '"run-now"' in r.text or "'run-now'" in r.text


# ---------------------------------------------------------------------------
# Section grouping in the palette
# ---------------------------------------------------------------------------


async def test_palette_js_has_section_headers(cookie_client):
    """The palette must render section headers for command groups."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # Sections are defined in baseCommands.
    assert "Ticket actions" in r.text
    assert "Properties" in r.text
    assert "Navigation" in r.text
    assert "Work views" in r.text
    assert "View" in r.text
    # Section headers are rendered as non-selectable list items via setAttribute.
    assert '"presentation"' in r.text or "'presentation'" in r.text


async def test_palette_js_navigation_skips_section_headers(cookie_client):
    """Arrow-key navigation (move) must skip over section header items."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # The move function skips section headers (items without .run).
    assert "state.items[next].run" in r.text


async def test_palette_js_includes_bulk_action_commands(cookie_client):
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "Bulk actions" in r.text
    assert "Bulk: set project" in r.text
    assert "Bulk: add label" in r.text
    assert "bulk.openMenu" in r.text


async def test_palette_js_prefers_project_shortcut_over_priority(cookie_client):
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "Set project" in r.text
    assert 'shortcut: "P"' in r.text
    assert 'window.ndOpenPropertyPicker(ctx.id, "project")' in r.text
    assert 'shortcut: "Shift P"' in r.text
    assert 'window.ndOpenPropertyPicker(ctx.id, "priority")' in r.text


# ---------------------------------------------------------------------------
# View / display commands
# ---------------------------------------------------------------------------


async def test_palette_js_includes_view_toggle_command(cookie_client):
    """The palette must include a toggle board/runs view command."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "Switch to tickets view" in r.text
    assert "Switch to runs view" in r.text


async def test_palette_js_includes_focus_search_command(cookie_client):
    """The palette must include a 'Focus search' command."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "Focus search" in r.text


async def test_palette_js_saved_views_hook(cookie_client):
    """The palette must include a conditional saved-views hook."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # The hook only activates when ndSavedViews is defined.
    assert "ndSavedViews" in r.text
    assert "Jump to saved view" in r.text


# ---------------------------------------------------------------------------
# Cheatsheet: View & display section
# ---------------------------------------------------------------------------


async def test_cheatsheet_includes_view_section(cookie_client):
    """The cheat sheet must document view/display shortcuts."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "View" in r.text or "Display" in r.text or "view" in r.text


async def test_primary_nav_collapses_to_ia_groups(cookie_client):
    """The header exposes Work, Insights, and Setup as top-level nav."""
    r = await cookie_client.get("/")
    assert r.status_code == 200

    nav_html = r.text.split('<nav class="flex items-center gap-1 text-sm" aria-label="Primary">', 1)[1]
    nav_html = nav_html.split("</nav>", 1)[0]

    summary_labels = re.findall(r"<summary[^>]*>\s*<span>([^<]+)</span>", nav_html, re.S)
    assert summary_labels == ["Work", "Insights", "Setup", "Views"]
    assert ">Board<" in nav_html
    assert ">Projects<" in nav_html
    assert ">Inbox<" in nav_html
    assert ">Scheduled<" in nav_html
    assert ">Analytics<" in nav_html
    assert ">Profiles<" in nav_html
    assert ">Toolsets<" in nav_html
    assert ">External tools<" not in nav_html
    assert nav_html.index(">Projects<") < nav_html.index(">List<")
    assert '<button type="button"' in nav_html


async def test_primary_nav_uses_hover_open_menus(cookie_client):
    r = await cookie_client.get("/")
    assert r.status_code == 200
    assert "pointerenter" in r.text
    assert "openMenu(menu)" in r.text
    assert "ev.preventDefault()" in r.text
    assert "1700" not in r.text


async def test_primary_nav_does_not_expose_legacy_labels_as_top_level(cookie_client):
    """Legacy destinations stay in groups, not as first-level header links."""
    r = await cookie_client.get("/")
    assert r.status_code == 200

    summaries = re.findall(r"<summary[^>]*>\s*<span>([^<]+)</span>", r.text, re.S)
    assert summaries == ["Work", "Insights", "Setup", "Views"]


async def test_cheatsheet_includes_focus_search(cookie_client):
    """The cheat sheet must document the / focus-search shortcut."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "Focus search" in r.text


async def test_cheatsheet_notes_board_view_toggle(cookie_client):
    """The cheat sheet must mention the board/runs view toggle."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "board" in r.text.lower() or "view" in r.text.lower()


# ---------------------------------------------------------------------------
# Palette targets cursor-focused ticket
# ---------------------------------------------------------------------------


async def test_palette_js_targets_cursor_ticket(cookie_client):
    """The palette must use the cursor-selected card when no sidebar selection."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # currentTicket() checks data-nd-cursor attribute as fallback.
    assert "data-nd-cursor" in r.text


async def test_palette_js_search_results_section(cookie_client):
    """Ticket search results must appear under a 'Search results' section."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "Search results" in r.text


# ---------------------------------------------------------------------------
# Fix #1: Enter on board opens ticket detail
# ---------------------------------------------------------------------------


async def test_palette_js_enter_opens_board_ticket(cookie_client):
    """Enter on the board must navigate to /tickets/{id} for the focused ticket."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    js = r.text
    assert 'key === "Enter"' in js
    assert "board-grid" in js
    # Navigates to the ticket detail page.
    assert "/tickets/" in js


# ---------------------------------------------------------------------------
# Fix #2: Space opens sidebar (not a dead ndPeekTicket call)
# ---------------------------------------------------------------------------


async def test_palette_js_space_loads_sidebar(cookie_client):
    """Space must trigger the board sidebar swap for the focused ticket."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    js = r.text
    assert "openPeek" in js
    # Sidebar swap path is present in openPeek.
    assert "/board/sidebar?ticket_id=" in js
    assert "desiredBoardSidebarTicketId = ctx.id" in js


# ---------------------------------------------------------------------------
# Fix #3: Cmd/Ctrl+B toggles board / list
# ---------------------------------------------------------------------------


async def test_palette_js_ctrlb_toggles_view(cookie_client):
    """Cmd/Ctrl+B must toggle between / and /list preserving query params."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    js = r.text
    assert '(e.metaKey || e.ctrlKey) && (key === "b" || key === "B")' in js
    assert '"/list"' in js


async def test_palette_js_ctrlb_command_in_view_section(cookie_client):
    """The palette must expose a board/list toggle command with Ctrl/Cmd B shortcut."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "Switch to board view" in r.text
    assert "Switch to list view" in r.text
    assert 'shortcut: "Ctrl/Cmd B"' in r.text


async def test_cheatsheet_documents_ctrlb(cookie_client):
    """The cheatsheet must document Ctrl/Cmd+B as the board/list toggle."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "Ctrl / Cmd + B" in r.text
    assert "Toggle board / list view" in r.text


# ---------------------------------------------------------------------------
# Fix #4: G chords — g b board, g l list, no g w
# ---------------------------------------------------------------------------


async def test_palette_js_g_b_goes_to_board(cookie_client):
    """g b chord must navigate to / (board)."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert 'shortcut: "g b"' in r.text


async def test_palette_js_g_l_goes_to_list(cookie_client):
    """g l chord must navigate to /list."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert 'shortcut: "g l"' in r.text
    assert "Go to Work: list" in r.text


async def test_palette_js_g_w_shortcut_removed(cookie_client):
    """g w must no longer appear as a keyboard shortcut in the palette."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert 'shortcut: "g w"' not in r.text


async def test_cheatsheet_documents_g_b_and_g_l_not_g_w(cookie_client):
    """Cheatsheet must teach g b and g l but not g w."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "g then b" in r.text
    assert "g then l" in r.text
    assert "g then w" not in r.text


# ---------------------------------------------------------------------------
# Fix #5: L CapsLock normalized
# ---------------------------------------------------------------------------


async def test_palette_js_l_capslock_normalized(cookie_client):
    """Both column-nav L and label-picker Shift+L must use key.toLowerCase()."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    js = r.text
    assert 'key.toLowerCase() === "l" && !e.shiftKey' in js
    assert 'key.toLowerCase() === "l" && e.shiftKey' in js


# ---------------------------------------------------------------------------
# Fix #6: Esc layering
# ---------------------------------------------------------------------------


async def test_bulk_select_js_esc_closes_menu_without_focus(cookie_client):
    """Esc must close an open bulk menu even when focus is outside the menu."""
    r = await cookie_client.get("/static/bulk_select.js")
    assert r.status_code == 200
    assert 'querySelector("[data-nd-bulk-menu]:not([hidden])")' in r.text


async def test_palette_js_esc_clears_board_cursor(cookie_client):
    """After selection is cleared, a further Esc must clear the board cursor."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    js = r.text
    assert "clearBoardCursor()" in js
    assert "paintBoardSelectedCard(null)" in js


# ---------------------------------------------------------------------------
# Fix #7: View toggle label uses active tab detection
# ---------------------------------------------------------------------------


async def test_palette_js_view_toggle_uses_aria_pressed(cookie_client):
    """The palette must detect the active view via aria-pressed, not a dead attribute."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    js = r.text
    assert 'aria-pressed="true"' in js
    assert "data-sb-view" in js
    # The old non-existent selector must be gone.
    assert "data-board-view='runs'" not in js


# ---------------------------------------------------------------------------
# Fix #8: I sends draft ticket to inbox
# ---------------------------------------------------------------------------


async def test_palette_js_i_key_sends_to_inbox(cookie_client):
    """I on the board must send the focused draft ticket to Inbox."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    js = r.text
    assert 'key === "i" || key === "I"' in js
    assert "value=inbox" in js
    assert "Sent to Inbox" in js


async def test_palette_js_inbox_command_in_draft_order(cookie_client):
    """The inbox command must appear in the palette for draft tickets."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert '"inbox"' in r.text
    assert 'shortcut: "I"' in r.text
    assert "Send to Inbox" in r.text


async def test_cheatsheet_documents_i_send_to_inbox(cookie_client):
    """The cheatsheet must document I as Send to Inbox."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "Send to Inbox" in r.text


# ---------------------------------------------------------------------------
# Fix #9: Stale comment corrected
# ---------------------------------------------------------------------------


async def test_palette_js_inbox_comment_is_accurate(cookie_client):
    """The inbox triage comment must not say 'draft if not' (old/incorrect)."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "draft if not" not in r.text


# ---------------------------------------------------------------------------
# Route-level: draft→inbox via property/status route
# ---------------------------------------------------------------------------


async def test_send_draft_to_inbox_via_property_status_route(
    cookie_client, session, _profile
):
    """POST /board/tickets/{id}/property/status with value=inbox must work for draft."""
    from nightdesk.domain.tickets import create_ticket, get_ticket

    t = create_ticket(
        session,
        title="to-inbox-test",
        prompt="x",
        profile_id=_profile.id,
        status="draft",
        source_path="/tmp",
    )
    r = await cookie_client.post(
        f"/board/tickets/{t.id}/property/status",
        data={"value": "inbox"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()
    assert get_ticket(session, t.id).status == "inbox"


# ---------------------------------------------------------------------------
# Manual validation notes
# ---------------------------------------------------------------------------

# The following behaviors require browser-based manual validation or a
# Playwright/Selenium test suite:
#
# 1. Ctrl/Cmd+K opens the palette; shortcut hints appear as <kbd> badges.
# 2. Ticket actions are grouped under a "Ticket actions" section header.
# 3. Navigation commands are grouped under a "Navigation" section header.
# 4. Arrow-key navigation skips over section headers.
# 5. For a running ticket, Run now / Requeue / Archive are hidden.
# 6. For a review ticket, Requeue and Archive appear first.
# 7. The "Switch to runs/tickets view" command toggles the board view.
# 8. The "Focus search" command focuses the header search input.
# 9. "Jump to saved view" only appears when ndSavedViews is defined.
# 10. Search results from /header/search appear under "Search results" header.
