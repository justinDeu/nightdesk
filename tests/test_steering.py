"""Domain tests for the mid-run steering queue (``domain/steering.py``).

Covers CRUD guards, claim atomicity/ordering, mark-delivered, and the
run-completion drain (folds remaining bodies into next_run_context, cancels the
rows). All at the domain layer — no worker, no HTTP.
"""
from __future__ import annotations

import pytest

from nightdesk.db.models import Run
from nightdesk.domain.conversations import create_conversation
from nightdesk.domain.runs import start_run
from nightdesk.domain.tickets import create_ticket, get_ticket
from nightdesk.domain.steering import (
    InvalidSteerState, SteerMessageNotFound,
    add_steer_message, cancel_steer_message, claim_next_steer_message,
    drain_pending_to_context, edit_steer_message, list_steer_messages,
    mark_delivered, reorder_steer_messages,
)


def _ticket(session, sample_profile, *, status="running"):
    return create_ticket(
        session, title="t", prompt="do it", priority=0,
        profile_id=sample_profile.id, source_path="/tmp", status=status,
    )


def _conversation(session, ticket):
    conv = create_conversation(
        session, ticket_id=ticket.id, profile_id=ticket.profile_id,
        backend="opencode", transcript_path="/t.log",
    )
    ticket.current_conversation_id = conv.id
    session.commit()
    return conv


def _add(session, conv, ticket, body, **kw):
    return add_steer_message(
        session, conversation_id=conv.id, ticket_id=ticket.id, body=body, **kw,
    )


def test_add_lists_in_order_and_assigns_positions(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    a = _add(session, conv, t, "first")
    b = _add(session, conv, t, "second")
    assert a.position == 0 and b.position == 1
    msgs = list_steer_messages(session, conv.id)
    assert [m.body for m in msgs] == ["first", "second"]


def test_add_rejects_empty_body(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    with pytest.raises(ValueError):
        _add(session, conv, t, "   ")


def test_add_rejects_bad_delivery_mode(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    with pytest.raises(ValueError):
        _add(session, conv, t, "x", delivery_mode="nonsense")


def test_edit_only_pending(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    m = _add(session, conv, t, "before")
    edit_steer_message(session, m.id, body="after")
    assert get_ticket(session, t.id) is not None
    assert list_steer_messages(session, conv.id)[0].body == "after"
    # Once claimed (delivering), edit is refused.
    claim_next_steer_message(session, conv.id)
    with pytest.raises(InvalidSteerState):
        edit_steer_message(session, m.id, body="too late")


def test_edit_missing_raises(session, sample_profile):
    with pytest.raises(SteerMessageNotFound):
        edit_steer_message(session, "nope", body="x")


def test_cancel_is_idempotent_but_guards_delivering(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    m = _add(session, conv, t, "drop me")
    cancel_steer_message(session, m.id)
    # Cancelling again is a no-op, not an error.
    again = cancel_steer_message(session, m.id)
    assert again.state == "cancelled"
    # A delivering message cannot be cancelled.
    m2 = _add(session, conv, t, "in flight")
    claim_next_steer_message(session, conv.id)
    with pytest.raises(InvalidSteerState):
        cancel_steer_message(session, m2.id)


def test_reorder_pending(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    a = _add(session, conv, t, "a")
    b = _add(session, conv, t, "b")
    c = _add(session, conv, t, "c")
    reorder_steer_messages(session, conv.id, [c.id, a.id, b.id])
    assert [m.body for m in list_steer_messages(session, conv.id)] == ["c", "a", "b"]


def test_reorder_refuses_non_pending(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    a = _add(session, conv, t, "a")
    b = _add(session, conv, t, "b")
    claim_next_steer_message(session, conv.id)  # a -> delivering
    with pytest.raises(InvalidSteerState):
        reorder_steer_messages(session, conv.id, [b.id, a.id])


def test_claim_is_ordered_and_atomic(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    _add(session, conv, t, "first")
    _add(session, conv, t, "second")
    c1 = claim_next_steer_message(session, conv.id)
    assert c1.body == "first" and c1.state == "delivering"
    c2 = claim_next_steer_message(session, conv.id)
    assert c2.body == "second"
    # Nothing pending left to claim.
    assert claim_next_steer_message(session, conv.id) is None
    # A second claim of the same row can't happen: only pending rows are claimed.
    assert {m.state for m in list_steer_messages(session, conv.id)} == {"delivering"}


def test_mark_delivered(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    r = start_run(session, ticket_id=t.id, worktree_path="/w",
                  transcript_path="/t.log", pid=None, host="h",
                  conversation_id=conv.id)
    m = _add(session, conv, t, "hi")
    claim_next_steer_message(session, conv.id)
    mark_delivered(session, m.id, run_id=r.id)
    session.refresh(m)
    assert m.state == "delivered"
    assert m.delivered_run_id == r.id
    assert m.delivered_at is not None
    # Delivered rows are not in the active (pending/delivering) list.
    assert list_steer_messages(session, conv.id) == []


def test_drain_folds_into_context_and_cancels(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    _add(session, conv, t, "do X")
    _add(session, conv, t, "then Y")
    drained = drain_pending_to_context(session, t.id, conv.id)
    assert drained == "do X\n\nthen Y"
    t2 = get_ticket(session, t.id)
    assert t2.next_run_context == "do X\n\nthen Y"
    assert t2.next_run_context_updated_at is not None
    # The rows are cancelled (removed from the live queue).
    assert list_steer_messages(session, conv.id) == []
    assert {m.state for m in list_steer_messages(session, conv.id, states=("cancelled",))} == {"cancelled"}


def test_drain_appends_to_existing_context(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    t.next_run_context = "existing note"
    session.commit()
    _add(session, conv, t, "new steer")
    drain_pending_to_context(session, t.id, conv.id)
    assert get_ticket(session, t.id).next_run_context == "existing note\n\nnew steer"


def test_drain_includes_delivering_residue(session, sample_profile):
    """A message claimed (delivering) but never confirmed delivered is folded in
    too — nothing silently dropped when a run ends mid-flight."""
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    _add(session, conv, t, "claimed but not delivered")
    claim_next_steer_message(session, conv.id)
    drained = drain_pending_to_context(session, t.id, conv.id)
    assert drained == "claimed but not delivered"


def test_drain_empty_returns_none(session, sample_profile):
    t = _ticket(session, sample_profile)
    conv = _conversation(session, t)
    assert drain_pending_to_context(session, t.id, conv.id) is None
    assert get_ticket(session, t.id).next_run_context is None
