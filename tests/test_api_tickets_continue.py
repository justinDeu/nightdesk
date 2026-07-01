"""JSON API tests for the continue / new-conversation endpoints.

These mirror the HTMX continue/new-conversation behavior (see
``tests/test_ticket_page.py``) but over the bearer-auth ``/api/v1`` surface,
proving scripts/agents can resume a conversation (continue) or start a fresh
one (new-conversation) without a browser. The SDK resume itself is exercised
end-to-end elsewhere (``tests/test_run_one_workspaces.py``); here we assert the
*staging* the worker consumes — the ``continue`` run intent, the resumed
conversation id + its session id — not just a status flip.
"""
from __future__ import annotations

import pytest

from nightdesk.domain.conversations import active_conversation
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import create_ticket, get_ticket, transition_status


def _make_profile(session, **overrides):
    from nightdesk.domain.profiles import create_profile
    fields = dict(
        name="p", fs_read=[], fs_write=["/opt/code"], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    fields.update(overrides)
    return create_profile(session, **fields)


def _review_ticket_with_prior_turn(session, *, session_id):
    """A ticket in `review` whose active conversation has one finished turn.

    ``session_id`` is the resume handle lifted onto the conversation; pass None
    to model a non-resumable (crashed-before-session) conversation.
    """
    p = _make_profile(session)
    t = create_ticket(session, title="rerun", prompt="fix it", priority=0,
                      profile_id=p.id, status="queued", run_now=False,
                      source_path="/tmp")
    transition_status(session, t.id, "running")
    prior = start_run(session, ticket_id=t.id, worktree_path="/tmp/w",
                      transcript_path="/tmp/p.log", pid=None, host="h")
    finish_run(session, prior.id, exit_status="success", error_summary=None,
               session_id=session_id)
    transition_status(session, t.id, "review")
    return t


async def test_continue_on_resumable_conversation_stages_resumed_run(client, session):
    """Continue with a message stages a CONTINUE run that targets the active
    conversation's session id (verified via the staged intent + conversation id
    + the resumed session id), not merely a status change."""
    t = _review_ticket_with_prior_turn(session, session_id="sess-prior-1")

    r = await client.post(f"/api/v1/tickets/{t.id}/continue",
                          json={"message": "now also fix the touch variant"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["run_now"] is True

    session.expire_all()
    refreshed = get_ticket(session, t.id)
    overrides = refreshed.permission_overrides or {}
    # The worker consumes these to seed resume=<session id>.
    assert overrides["nightdesk_run_intent"] == "continue"
    conv = active_conversation(session, refreshed)
    assert overrides["nightdesk_conversation_id"] == conv.id
    # The message is carried as the next user turn.
    assert refreshed.next_run_context == "now also fix the touch variant"
    # The resumed handle is the conversation's session id (not None), proving a
    # genuine session resume is staged, not a fresh-context run.
    assert conv.session_id == "sess-prior-1"


async def test_continue_refuses_null_session_conversation(client, session):
    """A conversation whose turn never recorded a session id is not resumable:
    Continue returns 409 naming the problem and pointing at new-conversation."""
    t = _review_ticket_with_prior_turn(session, session_id=None)

    r = await client.post(f"/api/v1/tickets/{t.id}/continue",
                          json={"message": "keep going"})
    assert r.status_code == 409
    detail = r.json()["detail"].lower()
    assert "session" in detail or "resumable" in detail or "conversation" in detail
    assert "new conversation" in detail


async def test_continue_wrong_status_is_409(client, session):
    """Continue is only valid from review/archived; a draft ticket has no
    finished conversation to continue from."""
    p = _make_profile(session)
    t = create_ticket(session, title="draft", prompt="p", priority=0,
                      profile_id=p.id, status="draft", source_path="/tmp")
    r = await client.post(f"/api/v1/tickets/{t.id}/continue",
                          json={"message": "hi"})
    assert r.status_code == 409


async def test_continue_not_found(client, session):
    r = await client.post("/api/v1/tickets/does-not-exist/continue",
                          json={"message": "hi"})
    assert r.status_code == 404


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
async def test_continue_empty_message_is_422(client, session, bad):
    """Empty / whitespace-only message has nothing to append -> 422."""
    t = _review_ticket_with_prior_turn(session, session_id="sess-x")
    r = await client.post(f"/api/v1/tickets/{t.id}/continue",
                          json={"message": bad})
    assert r.status_code == 422


async def test_new_conversation_creates_fresh_conversation(client, session):
    """New-conversation stages a brand-new conversation (new session, no resumed
    history) and queues a run-now."""
    t = _review_ticket_with_prior_turn(session, session_id="sess-prior-1")
    old_conv_id = t.current_conversation_id

    r = await client.post(f"/api/v1/tickets/{t.id}/new-conversation",
                          json={"message": "try again fresh", "workspace": "keep"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["run_now"] is True

    session.expire_all()
    refreshed = get_ticket(session, t.id)
    overrides = refreshed.permission_overrides or {}
    assert overrides.get("nightdesk_new_conversation") is True
    # No resume handle staged: a new conversation does NOT thread a session id.
    assert overrides.get("nightdesk_conversation_id") is None
    assert overrides["nightdesk_run_intent"] in ("retry", "restart")
    assert refreshed.next_run_context == "try again fresh"
    # The prior conversation is untouched; the worker will create a new one.
    assert refreshed.current_conversation_id == old_conv_id


async def test_new_conversation_switches_runtime(client, session):
    """new-conversation with a profile_id switches the ticket's runtime for the
    next run (switching runtime always starts a new conversation)."""
    p1 = _make_profile(session, name="claude")
    p2 = _make_profile(session, name="opencode", backend="opencode")
    t = create_ticket(session, title="switch", prompt="p", priority=0,
                      profile_id=p1.id, status="queued", run_now=False,
                      source_path="/tmp")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")

    r = await client.post(f"/api/v1/tickets/{t.id}/new-conversation",
                          json={"profile_id": p2.id, "workspace": "fresh"})
    assert r.status_code == 200, r.text
    assert r.json()["profile_id"] == p2.id

    session.expire_all()
    refreshed = get_ticket(session, t.id)
    overrides = refreshed.permission_overrides or {}
    assert overrides.get("nightdesk_new_conversation") is True
    # fresh workspace -> restart intent.
    assert overrides["nightdesk_run_intent"] == "restart"


async def test_new_conversation_not_found(client, session):
    r = await client.post("/api/v1/tickets/does-not-exist/new-conversation",
                          json={"workspace": "keep"})
    assert r.status_code == 404


async def test_new_conversation_unknown_profile_is_404(client, session):
    """An unknown profile_id is a 404 (FastAPI detail), not a 500."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p", priority=0,
                      profile_id=p.id, status="queued", run_now=False,
                      source_path="/tmp")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    r = await client.post(f"/api/v1/tickets/{t.id}/new-conversation",
                          json={"profile_id": "no-such-profile"})
    assert r.status_code == 404
