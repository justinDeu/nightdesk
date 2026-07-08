"""Mid-run steering queue: follow-ups the user types while a run is live.

A :class:`~nightdesk.db.models.SteerMessage` belongs to a Conversation and is
delivered into the SAME live run (inject-capable backends read the queue via
the host watcher) or the NEXT turn (queue-only backends, via
:func:`drain_pending_to_context` + an auto-continue at run completion).

This module owns the queue's write path and its guards. The live-run watcher
(``executors/local.py``) claims messages with :func:`claim_next_steer_message`
and confirms delivery with :func:`mark_delivered`; the run-completion drain
(``worker/run_one.py``) folds anything still queued into ``next_run_context``
with :func:`drain_pending_to_context`. See
``docs/design/session-suite/mid-run-steering.md``.

Relationship to ``next_run_context``: SteerMessage is the LIVE-run queue;
``next_run_context`` is the AT-REST staged note. They converge at exactly one
point — the run-completion drain — which reuses the existing, tested
``continue_ticket`` machinery. The ``/continue``/``/resume``/``/retry``/
``/restart``/``/new-conversation`` flows are untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from nightdesk.db.models import SteerMessage


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SteerMessageNotFound(Exception):
    pass


class InvalidSteerState(Exception):
    """A guarded mutation (edit/reorder/cancel) hit a message that is no longer
    ``pending`` — it has already started delivering or is delivered/cancelled.
    Route handlers map this to HTTP 409."""


def add_steer_message(
    session: Session, *, conversation_id: str, ticket_id: str, body: str,
    delivery_mode: str = "at_turn",
) -> SteerMessage:
    """Append a follow-up to the conversation's live queue (tail position)."""
    clean = (body or "").strip()
    if not clean:
        raise ValueError("steer message body cannot be empty")
    if delivery_mode not in ("at_turn", "inject"):
        raise ValueError("delivery_mode must be 'at_turn' or 'inject'")
    max_pos = session.scalar(
        select(func.max(SteerMessage.position)).where(
            SteerMessage.conversation_id == conversation_id,
            SteerMessage.state.in_(("pending", "delivering")),
        )
    )
    position = (max_pos + 1) if max_pos is not None else 0
    m = SteerMessage(
        conversation_id=conversation_id,
        ticket_id=ticket_id,
        body=clean,
        position=position,
        state="pending",
        delivery_mode=delivery_mode,
    )
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def list_steer_messages(
    session: Session, conversation_id: str, *,
    states: Sequence[str] = ("pending", "delivering"),
) -> list[SteerMessage]:
    """The conversation's queue in delivery order (position, then created_at)."""
    return list(session.scalars(
        select(SteerMessage)
        .where(
            SteerMessage.conversation_id == conversation_id,
            SteerMessage.state.in_(tuple(states)),
        )
        .order_by(SteerMessage.position, SteerMessage.created_at)
    ))


def edit_steer_message(session: Session, message_id: str, *, body: str) -> SteerMessage:
    """Rewrite a still-``pending`` message's body."""
    clean = (body or "").strip()
    if not clean:
        raise ValueError("steer message body cannot be empty")
    m = session.get(SteerMessage, message_id)
    if m is None:
        raise SteerMessageNotFound(message_id)
    if m.state != "pending":
        raise InvalidSteerState(f"cannot edit a {m.state} steer message")
    m.body = clean
    session.commit()
    session.refresh(m)
    return m


def reorder_steer_messages(
    session: Session, conversation_id: str, ordered_ids: Sequence[str],
) -> list[SteerMessage]:
    """Reassign queue positions to match ``ordered_ids``. All referenced
    messages must be ``pending`` (a message that has started delivering can no
    longer be reordered)."""
    rows = list(session.scalars(
        select(SteerMessage).where(
            SteerMessage.conversation_id == conversation_id,
            SteerMessage.id.in_(tuple(ordered_ids)),
        )
    ))
    by_id = {r.id: r for r in rows}
    for mid in ordered_ids:
        r = by_id.get(mid)
        if r is None:
            raise SteerMessageNotFound(mid)
        if r.state != "pending":
            raise InvalidSteerState(f"cannot reorder a {r.state} steer message")
    for pos, mid in enumerate(ordered_ids):
        by_id[mid].position = pos
    session.commit()
    return list_steer_messages(session, conversation_id)


def cancel_steer_message(session: Session, message_id: str) -> SteerMessage:
    """Remove a still-``pending`` message from the queue (``pending -> cancelled``)."""
    m = session.get(SteerMessage, message_id)
    if m is None:
        raise SteerMessageNotFound(message_id)
    if m.state == "cancelled":
        return m  # idempotent
    if m.state != "pending":
        raise InvalidSteerState(f"cannot cancel a {m.state} steer message")
    m.state = "cancelled"
    session.commit()
    session.refresh(m)
    return m


def claim_next_steer_message(session: Session, conversation_id: str) -> Optional[SteerMessage]:
    """Atomically claim the next pending message for delivery (``pending ->
    delivering``). Ordered SELECT + state-conditional UPDATE so two watchers
    (or a watcher racing the drain) never claim the same row: the loser sees
    ``rowcount == 0`` and gets None back, retrying on its next poll."""
    candidate = session.scalar(
        select(SteerMessage)
        .where(
            SteerMessage.conversation_id == conversation_id,
            SteerMessage.state == "pending",
        )
        .order_by(SteerMessage.position, SteerMessage.created_at)
        .limit(1)
    )
    if candidate is None:
        return None
    result = session.execute(
        update(SteerMessage)
        .where(SteerMessage.id == candidate.id, SteerMessage.state == "pending")
        .values(state="delivering")
    )
    session.commit()
    if result.rowcount == 0:
        return None
    session.refresh(candidate)
    return candidate


def mark_delivered(session: Session, message_id: str, *, run_id: str) -> None:
    """Confirm a claimed message was delivered into ``run_id`` (``delivering ->
    delivered``)."""
    session.execute(
        update(SteerMessage)
        .where(SteerMessage.id == message_id)
        .values(state="delivered", delivered_run_id=run_id, delivered_at=_now())
    )
    session.commit()


def drain_pending_to_context(
    session: Session, ticket_id: str, conversation_id: str,
) -> Optional[str]:
    """Fold every still-queued (pending or delivering) message into the ticket's
    ``next_run_context`` and cancel the rows. Returns the drained text (or None
    when the queue was empty).

    Called at run completion on a queue-only backend, or for any residue an
    inject-capable backend did not consume before the run ended. The drained
    text then drives the NEXT turn — either as the auto-continue's
    ``continue_message`` (resumable conversation) or as the visible "Guidance
    staged" chip (non-resumable). Nothing is silently dropped.
    """
    rows = list(session.scalars(
        select(SteerMessage)
        .where(
            SteerMessage.conversation_id == conversation_id,
            SteerMessage.state.in_(("pending", "delivering")),
        )
        .order_by(SteerMessage.position, SteerMessage.created_at)
    ))
    if not rows:
        return None
    bodies = [r.body.strip() for r in rows if r.body.strip()]
    drained = "\n\n".join(bodies) if bodies else None
    if drained:
        from nightdesk.domain.tickets import get_ticket
        t = get_ticket(session, ticket_id)
        existing = (t.next_run_context or "").rstrip()
        t.next_run_context = (existing + "\n\n" + drained) if existing else drained
        t.next_run_context_updated_at = _now()
    for r in rows:
        r.state = "cancelled"
    session.commit()
    return drained
