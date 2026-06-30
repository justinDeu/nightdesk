"""Domain-level tests for the Conversation/Turn model.

Covers the acceptance behaviors that live in the domain layer:
- a Ticket's first run creates a Conversation; subsequent bare runs join it;
- finish_run lifts the authoritative session_id onto the conversation and
  syncs cumulative totals;
- conversation cost/token totals equal the LAST turn's reported totals, NOT a
  sum (the SDK reports cumulative-since-process-start tokens);
- latest_turn resolves the active conversation's last turn by position;
- continue refuses a null-session conversation and re-activates a selected one.
"""
from __future__ import annotations

import pytest

from nightdesk.db.models import Run, Ticket
from nightdesk.domain.conversations import (
    active_conversation, create_conversation, latest_turn,
    set_conversation_session, sync_conversation_from_turn,
)
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import (
    ConversationNotResumable, continue_ticket, create_ticket, transition_status,
)


def _ticket(session, sample_profile, *, status="queued"):
    return create_ticket(
        session, title="t", prompt="do it", priority=0,
        profile_id=sample_profile.id, source_path="/tmp", status=status,
    )


def test_first_run_creates_conversation_and_sets_current(session, sample_profile):
    t = _ticket(session, sample_profile)
    r = start_run(session, ticket_id=t.id, worktree_path="/w",
                  transcript_path="/t.log", pid=None, host="h")
    session.refresh(t)
    assert r.conversation_id is not None
    assert t.current_conversation_id == r.conversation_id
    assert r.position == 0
    conv = active_conversation(session, t)
    assert conv is not None
    assert conv.transcript_path == "/t.log"
    assert conv.backend == "claude_sdk"


def test_bare_second_run_joins_same_conversation(session, sample_profile):
    """Two directly-created runs (no explicit conversation) are turns of ONE
    conversation: the model's core 'one conversation, many turns' invariant."""
    t = _ticket(session, sample_profile)
    r1 = start_run(session, ticket_id=t.id, worktree_path="/w",
                   transcript_path="/t.log", pid=None, host="h")
    r2 = start_run(session, ticket_id=t.id, worktree_path="/w",
                   transcript_path="/t.log", pid=None, host="h")
    assert r1.conversation_id == r2.conversation_id
    assert r1.position == 0
    assert r2.position == 1
    assert latest_turn(session, r1.conversation_id).id == r2.id


def test_finish_run_lifts_session_id_and_syncs_status(session, sample_profile):
    t = _ticket(session, sample_profile)
    r = start_run(session, ticket_id=t.id, worktree_path="/w",
                  transcript_path="/t.log", pid=None, host="h")
    finish_run(session, r.id, exit_status="success", error_summary=None,
               session_id="sess-1")
    session.refresh(r)
    conv = active_conversation(session, t)
    session.refresh(conv)
    # Authoritative session_id lifted onto the conversation.
    assert conv.session_id == "sess-1"
    assert r.session_id == "sess-1"
    # A finished successful turn moves the conversation to awaiting_review.
    assert conv.status == "awaiting_review"
    assert conv.finished_at is not None


def test_conversation_cost_uses_last_turn_totals_not_sum(session, sample_profile):
    """Acceptance #7: Conversation.cost_usd and token totals equal the LAST
    turn's reported totals, NOT a sum. The SDK reports cumulative-since-process-
    start tokens (which after a resume include the replayed prefix), so summing
    per-turn would double-count cache-read."""
    t = _ticket(session, sample_profile)
    r1 = start_run(session, ticket_id=t.id, worktree_path="/w",
                   transcript_path="/t.log", pid=None, host="h")
    finish_run(session, r1.id, exit_status="success", error_summary=None,
               session_id="sess-1")
    r1.cost_usd = 1.0
    r1.input_tokens = 100
    r1.cache_read_tokens = 50
    sync_conversation_from_turn(session, r1)

    r2 = start_run(session, ticket_id=t.id, worktree_path="/w",
                   transcript_path="/t.log", pid=None, host="h")
    finish_run(session, r2.id, exit_status="success", error_summary=None,
               session_id="sess-1")
    r2.cost_usd = 2.5  # cumulative (includes replayed prefix)
    r2.input_tokens = 300  # cumulative
    r2.cache_read_tokens = 250  # cumulative
    sync_conversation_from_turn(session, r2)

    conv = active_conversation(session, t)
    session.refresh(conv)
    assert conv.cost_usd == 2.5          # last turn, not 1.0 + 2.5
    assert conv.input_tokens == 300      # last turn, not 100 + 300
    assert conv.cache_read_tokens == 250  # last turn, not 50 + 250


def test_set_conversation_session_idempotent_and_ignores_none(session, sample_profile):
    t = _ticket(session, sample_profile)
    r = start_run(session, ticket_id=t.id, worktree_path="/w",
                  transcript_path="/t.log", pid=None, host="h")
    conv = active_conversation(session, t)
    # None never clobbers.
    set_conversation_session(session, conv.id, None)
    session.refresh(conv)
    assert conv.session_id is None
    # Sets eagerly.
    set_conversation_session(session, conv.id, "early-sid")
    session.refresh(conv)
    assert conv.session_id == "early-sid"
    # Idempotent on repeat.
    set_conversation_session(session, conv.id, "early-sid")
    session.refresh(conv)
    assert conv.session_id == "early-sid"


def test_continue_refuses_null_session_conversation(session, sample_profile):
    t = _ticket(session, sample_profile)
    transition_status(session, t.id, "running")
    r = start_run(session, ticket_id=t.id, worktree_path="/w",
                  transcript_path="/t.log", pid=None, host="h")
    finish_run(session, r.id, exit_status="failed", error_summary="crashed",
               session_id=None)  # no session captured
    transition_status(session, t.id, "review")
    with pytest.raises(ConversationNotResumable):
        continue_ticket(session, t.id, next_run_context="keep going")


def test_continue_resumable_conversation_stages_conversation_id(session, sample_profile):
    t = _ticket(session, sample_profile)
    transition_status(session, t.id, "running")
    r = start_run(session, ticket_id=t.id, worktree_path="/w",
                  transcript_path="/t.log", pid=None, host="h")
    finish_run(session, r.id, exit_status="success", error_summary=None,
               session_id="sess-x")
    transition_status(session, t.id, "review")
    out = continue_ticket(session, t.id, next_run_context="more")
    conv = active_conversation(session, t)
    # Staged the conversation id so the worker resumes THIS conversation.
    assert out.permission_overrides["nightdesk_conversation_id"] == conv.id
    assert out.permission_overrides["nightdesk_run_intent"] == "continue"


def test_continue_reactivates_selected_older_conversation(session, sample_profile):
    """Selecting an older conversation and continuing re-activates it: it
    becomes current_conversation_id (acceptance #2)."""
    t = _ticket(session, sample_profile)
    # Conversation A (older, resumable): the ticket's first turn.
    transition_status(session, t.id, "running")
    ra = start_run(session, ticket_id=t.id, worktree_path="/w",
                   transcript_path="/ta.log", pid=None, host="h")
    finish_run(session, ra.id, exit_status="success", error_summary=None,
               session_id="sess-a")
    transition_status(session, t.id, "review")
    conv_a = ra.conversation_id

    # A NEW conversation B is created explicitly (a New-conversation action) and
    # becomes current; a turn runs in it.
    conv_b = create_conversation(
        session, ticket_id=t.id, profile_id=sample_profile.id,
        backend="claude_sdk", transcript_path="/tb.log",
    )
    t.current_conversation_id = conv_b.id
    session.commit()
    transition_status(session, t.id, "queued")
    transition_status(session, t.id, "running")
    rb = start_run(session, ticket_id=t.id, worktree_path="/w",
                   transcript_path="/tb.log", pid=None, host="h",
                   conversation_id=conv_b.id)
    finish_run(session, rb.id, exit_status="success", error_summary=None,
               session_id="sess-b")
    transition_status(session, t.id, "review")
    session.refresh(t)
    assert t.current_conversation_id == conv_b.id

    # Continue the OLDER conversation explicitly -> it re-activates as current.
    continue_ticket(session, t.id, next_run_context="back to a",
                    conversation_id=conv_a)
    session.refresh(t)
    assert t.current_conversation_id == conv_a
    assert t.permission_overrides["nightdesk_conversation_id"] == conv_a


def test_latest_turn_handles_no_conversation(session, sample_profile):
    assert latest_turn(session, None) is None
    t = _ticket(session, sample_profile)
    assert active_conversation(session, t) is None
