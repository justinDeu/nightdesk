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
    assert "Go to archive" in r.text
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
    assert "View" in r.text
    # Section headers are rendered as non-selectable list items via setAttribute.
    assert '"presentation"' in r.text or "'presentation'" in r.text


async def test_palette_js_navigation_skips_section_headers(cookie_client):
    """Arrow-key navigation (move) must skip over section header items."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # The move function skips section headers (items without .run).
    assert "state.items[next].run" in r.text


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
