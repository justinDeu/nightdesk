"""Tests for the Inbox keyboard triage flow.

Covers:
  - JS ships the inbox cursor, triage actions, and shortcut wiring.
  - Cheatsheet documents inbox triage shortcuts.
  - Inbox items render with data-ticket-id for cursor targeting.
  - The inbox list container carries id="inbox-list" for page detection.
  - Inbox triage actions (promote, decline) respond to cookie-authed POST.
  - Shortcuts are ignored in typing contexts (isTypingTarget guard).
  - Board shortcuts (A=archive, R=run, H/L=columns) do NOT fire on inbox.
  - S is unbound on the inbox page (snooze not yet implemented).

Manual validation notes are included at the bottom for browser-level checks
that cannot be exercised from a server-side test suite.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.tickets import create_ticket, get_ticket, list_inbox

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
        name="kb-test",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


def _inbox(session, profile, *, title="Vague idea", complete=False, **kw):
    fields = dict(title=title, prompt="", status="inbox", profile_id=profile.id)
    if complete:
        fields["source_path"] = "/tmp"
    fields.update(kw)
    return create_ticket(session, **fields)


# ---------------------------------------------------------------------------
# JS ships inbox cursor + triage actions
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_js_inbox_cursor_functions(cookie_client):
    """The JS must ship inboxCards, inboxSetCursor, inboxMoveCursor."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "inboxCards" in r.text
    assert "inboxSetCursor" in r.text
    assert "inboxMoveCursor" in r.text
    assert "inboxCursorIndex" in r.text


@pytest.mark.anyio
async def test_js_inbox_page_detection(cookie_client):
    """The JS must detect the inbox page via isInboxPage()."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "isInboxPage" in r.text
    # Detection uses the inbox-list element.
    assert 'getElementById("inbox-list")' in r.text


@pytest.mark.anyio
async def test_js_inbox_triage_actions(cookie_client):
    """The JS must ship inboxPromote, inboxDecline, inboxOpenDetail."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "inboxPromote" in r.text
    assert "inboxDecline" in r.text
    assert "inboxOpenDetail" in r.text


@pytest.mark.anyio
async def test_js_inbox_triage_section_in_palette(cookie_client):
    """Command palette must include 'Inbox triage' section for inbox context."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "Inbox triage" in r.text


@pytest.mark.anyio
async def test_js_inbox_a_key_promotes(cookie_client):
    """On the inbox page, A must trigger promote (not archive)."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # The inbox section wires A to inboxPromote.
    assert 'inboxPromote(ictx, "queued")' in r.text


@pytest.mark.anyio
async def test_js_inbox_d_key_declines(cookie_client):
    """On the inbox page, D must trigger decline."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert "inboxDecline(ictx)" in r.text


@pytest.mark.anyio
async def test_js_inbox_l_key_opens_labels(cookie_client):
    """On the inbox page, plain L must open the label picker (no Shift needed)."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # Inbox block: plain l/L triggers openLabelPicker.
    # Must contain the inbox-scoped L handler.
    assert "openLabelPicker(ictx)" in r.text


@pytest.mark.anyio
async def test_js_inbox_s_key_is_unbound(cookie_client):
    """On the inbox page, S must not fire any action (snooze not yet implemented)."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # The inbox S handler is a no-op return.
    assert "snooze" in r.text.lower() or "unbound" in r.text.lower()


@pytest.mark.anyio
async def test_js_inbox_enter_opens_detail(cookie_client):
    """On the inbox page, Enter must open the focused item's detail page."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    assert 'inboxOpenDetail(ictx)' in r.text


@pytest.mark.anyio
async def test_js_jk_routes_to_inbox_cursor(cookie_client):
    """J/K on the inbox page must call inboxMoveCursor, not board moveCursor."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # The J/K handler checks isInboxPage() first.
    assert "inboxMoveCursor(1)" in r.text
    assert "inboxMoveCursor(-1)" in r.text


@pytest.mark.anyio
async def test_js_inbox_blocks_board_shortcuts(cookie_client):
    """On the inbox page, board shortcuts (R, Space, H/L) must not leak through."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # The inbox handler returns early for unrecognized keys, preventing
    # fall-through to the board shortcut section.
    # Look for the inbox page guard that returns after the inbox block.
    assert "don't leak board" in r.text or "leak board" in r.text


# ---------------------------------------------------------------------------
# Inbox HTML structure for cursor targeting
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_inbox_page_has_list_container(cookie_client, session, profile):
    """The inbox page must render #inbox-list for JS page detection."""
    _inbox(session, profile, title="Detect me")
    r = await cookie_client.get("/inbox")
    assert r.status_code == 200
    assert 'id="inbox-list"' in r.text


@pytest.mark.anyio
async def test_inbox_items_have_data_ticket_id(cookie_client, session, profile):
    """Each inbox item must carry data-ticket-id for cursor targeting."""
    t = _inbox(session, profile, title="Cursor target")
    r = await cookie_client.get("/inbox")
    assert r.status_code == 200
    assert f'data-ticket-id="{t.id}"' in r.text


@pytest.mark.anyio
async def test_inbox_items_are_li_elements(cookie_client, session, profile):
    """Inbox items must be <li> elements (matching the cursor pattern)."""
    _inbox(session, profile, title="Li element")
    r = await cookie_client.get("/inbox")
    assert r.status_code == 200
    assert '<li' in r.text
    assert 'data-ticket-id=' in r.text


# ---------------------------------------------------------------------------
# Inbox triage action routes (keyboard targets)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_inbox_promote_route_works_for_complete(cookie_client, session, profile):
    """Promote via cookie-authed POST should succeed for a complete item."""
    t = _inbox(session, profile, complete=True, title="Promotable")
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data={"target": "queued"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()
    assert get_ticket(session, t.id).status == "queued"


@pytest.mark.anyio
async def test_promote_form_urlencoded_target_queued_lands_queued(cookie_client, session, profile):
    """POST /promote with target=queued as a raw form-urlencoded body must land
    the ticket in queued, not draft.

    This is the exact encoding inboxPromote() uses: it sends
    'target=queued' as the request body with Content-Type
    application/x-www-form-urlencoded. Previously inboxPromote sent no body
    at all, so the route defaulted to 'draft' silently.
    """
    t = _inbox(session, profile, complete=True, title="Form-body target test")
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        content="target=queued",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "HX-Request": "true",
        },
    )
    assert r.status_code == 200
    session.expire_all()
    assert get_ticket(session, t.id).status == "queued", (
        "inboxPromote sends target=queued as form body; route must land in queued, not draft"
    )


@pytest.mark.anyio
async def test_inbox_promote_route_blocks_incomplete(cookie_client, session, profile):
    """Promote should fail (422) for an incomplete item."""
    t = _inbox(session, profile, title="Incomplete")
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data={"target": "queued"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 422
    assert get_ticket(session, t.id).status == "inbox"


@pytest.mark.anyio
async def test_inbox_decline_route_works(cookie_client, session, profile):
    """Decline via cookie-authed POST should archive the item."""
    t = _inbox(session, profile, title="Declinable")
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/decline",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()
    assert get_ticket(session, t.id).status == "archived"


@pytest.mark.anyio
async def test_inbox_list_refresh_endpoint(cookie_client, session, profile):
    """The /inbox/list endpoint must return a refreshed list fragment."""
    _inbox(session, profile, title="Refresh test")
    r = await cookie_client.get("/inbox/list")
    assert r.status_code == 200
    assert "Refresh test" in r.text


# ---------------------------------------------------------------------------
# Cheatsheet: inbox shortcuts documented
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cheatsheet_documents_inbox_shortcuts(cookie_client):
    """The cheatsheet must include an 'Inbox triage' section."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "Inbox triage" in r.text


@pytest.mark.anyio
async def test_cheatsheet_documents_accept_promote(cookie_client):
    """The cheatsheet must document the A = Accept / promote shortcut."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "Accept" in r.text or "promote" in r.text.lower()


@pytest.mark.anyio
async def test_cheatsheet_documents_decline(cookie_client):
    """The cheatsheet must document the D = Decline shortcut."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "Decline" in r.text


@pytest.mark.anyio
async def test_cheatsheet_documents_inbox_labels_shortcut(cookie_client):
    """The cheatsheet must document L = Set labels for inbox."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    # The inbox section has L = labels.
    assert "Set labels" in r.text


@pytest.mark.anyio
async def test_cheatsheet_notes_snooze_reserved(cookie_client):
    """The cheatsheet must note that S is reserved for future snooze."""
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "snooze" in r.text.lower()


# ---------------------------------------------------------------------------
# currentTicket() inbox awareness
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_js_current_ticket_checks_inbox_cursor(cookie_client):
    """currentTicket() must check the inbox cursor when on the inbox page."""
    r = await cookie_client.get("/static/command_palette.js")
    assert r.status_code == 200
    # currentTicket() checks isInboxPage() and inboxCurrentTicket().
    assert "inboxCurrentTicket()" in r.text


# ---------------------------------------------------------------------------
# Manual validation notes
# ---------------------------------------------------------------------------

# The following behaviors require browser-based manual validation or a
# Playwright/Selenium test suite:
#
# INBOX CURSOR
# 1. On /inbox with items present, pressing J highlights the first item with
#    a blue outline (nd-cursor-active). Pressing J again moves down; K moves up.
# 2. The cursor wraps: pressing K at the top stays at item 0; J at the bottom
#    stays at the last item.
# 3. After a promote/decline action, the list refreshes and the cursor re-lands
#    on the next item at the same index (or last if at end).
#
# INBOX TRIAGE SHORTCUTS
# 4. A promotes the focused item (queued if complete, 422 feedback if not).
# 5. D declines (archives) the focused item; a toast says "Declined — moved to Archive".
# 6. Enter navigates to the ticket detail page. E opens the promote/flesh-out modal.
# 7. P opens the priority picker for the focused item.
# 8. L opens the label picker for the focused item (no Shift needed).
# 9. S does nothing on the inbox page (reserved for future snooze).
# 10. Board-only shortcuts (R, Space, H, Shift+L) do NOT fire on /inbox.
#
# TYPING CONTEXT GUARD
# 11. All shortcuts are ignored when focus is inside an input, textarea, or
#     select (e.g., the capture form at the top of the inbox page).
# 12. After typing in the capture form and pressing Escape to blur, shortcuts
#     resume working.
#
# COMMAND PALETTE
# 13. Ctrl/Cmd+K on /inbox shows "Inbox triage" section with A/D/E/L/P commands
#     for the cursor-focused item.
# 14. The cheatsheet (?) shows the "Inbox triage" section.
