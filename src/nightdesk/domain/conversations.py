"""Conversation domain helpers.

A Conversation is the primary unit of work: it owns Turns (the existing Run
rows) and the authoritative resume handle (``session_id``). These helpers are
the single place that resolves "the active conversation" and "its latest turn"
so callers (worker, API, board, dependency check) never reach past the model
hand-over-hand.

Cost/token totals on a Conversation are CUMULATIVE and equal the LAST turn's
reported totals — NOT a sum. The CC SDK reports cumulative-since-process-start
tokens, which after a resume include the replayed prefix, so summing per-turn
would double-count cache-read.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nightdesk.db.models import Conversation, Run, Ticket


class ConversationNotFound(Exception):
    pass


def create_conversation(
    session: Session,
    *,
    ticket_id: str,
    profile_id: Optional[str],
    backend: str,
    transcript_path: str,
    position: Optional[int] = None,
) -> Conversation:
    if position is None:
        position = next_conversation_position(session, ticket_id)
    conv = Conversation(
        ticket_id=ticket_id,
        profile_id=profile_id,
        backend=backend,
        status="active",
        started_at=datetime.now(timezone.utc),
        position=position,
        transcript_path=transcript_path,
    )
    session.add(conv)
    session.flush()
    return conv


def get_conversation(session: Session, conversation_id: str) -> Conversation:
    c = session.get(Conversation, conversation_id)
    if c is None:
        raise ConversationNotFound(conversation_id)
    return c


def list_conversations(session: Session, ticket_id: str) -> list[Conversation]:
    """A ticket's conversations, newest-first (highest position first)."""
    return list(session.scalars(
        select(Conversation)
        .where(Conversation.ticket_id == ticket_id)
        .order_by(Conversation.position.desc())
    ))


def active_conversation(session: Session, ticket: Ticket) -> Optional[Conversation]:
    """The ticket's active conversation, or None if it has never run."""
    cid = getattr(ticket, "current_conversation_id", None)
    if not cid:
        return None
    return session.get(Conversation, cid)


def latest_turn(session: Session, conversation_id: Optional[str]) -> Optional[Run]:
    """The most recent turn (Run) of a conversation by position, then started_at.

    ``conversation_id`` None → None. This is the single source of truth for
    "the active conversation's latest turn" used by continue-resume, the board
    outcome chip, dependency satisfaction, and the next_run_context handoff.
    """
    if not conversation_id:
        return None
    return session.scalar(
        select(Run)
        .where(Run.conversation_id == conversation_id)
        .order_by(Run.position.desc(), Run.started_at.desc())
        .limit(1)
    )


def next_conversation_position(session: Session, ticket_id: str) -> int:
    cur = session.scalar(
        select(func.coalesce(func.max(Conversation.position), -1))
        .where(Conversation.ticket_id == ticket_id)
    )
    return (cur or 0) + 1 if cur is not None and cur >= 0 else 0


def next_turn_position(session: Session, conversation_id: str) -> int:
    cur = session.scalar(
        select(func.coalesce(func.max(Run.position), -1))
        .where(Run.conversation_id == conversation_id)
    )
    return (cur or 0) + 1 if cur is not None and cur >= 0 else 0


def set_conversation_session(
    session: Session, conversation_id: Optional[str], session_id: Optional[str],
) -> None:
    """Eagerly persist the authoritative resume handle on the conversation.

    Called out of band the moment a session id is first observed (before the
    run finishes) so a cancel/crash after that point never leaves the
    conversation with a null session_id. Idempotent: a None session_id never
    clobbers an existing value.
    """
    if not conversation_id or not session_id:
        return
    conv = session.get(Conversation, conversation_id)
    if conv is None or conv.session_id == session_id:
        return
    conv.session_id = session_id
    session.commit()


def sync_conversation_from_turn(session: Session, run: Run) -> Optional[Conversation]:
    """After a turn updates/finishes, sync the conversation's cumulative totals.

    Per the cost rule, the conversation's totals equal the LAST turn's reported
    totals (not a sum). ``run`` is expected to be the latest turn of its
    conversation (the caller ensures this by passing the just-finished run).
    Also lifts the authoritative session_id off the turn and marks the
    conversation finished/awaiting_review when its latest turn finishes.
    """
    if run.conversation_id is None:
        return None
    conv = session.get(Conversation, run.conversation_id)
    if conv is None:
        return None
    # Cumulative totals == this (latest) turn's totals.
    conv.cost_usd = run.cost_usd
    conv.input_tokens = run.input_tokens
    conv.output_tokens = run.output_tokens
    conv.cache_read_tokens = run.cache_read_tokens
    conv.cache_write_tokens = run.cache_write_tokens
    conv.model_used = run.model_used
    # Authoritative session id lifted off the turn.
    if run.session_id:
        conv.session_id = run.session_id
    if run.finished_at is not None:
        conv.finished_at = run.finished_at
        conv.status = "awaiting_review" if run.exit_status else "done"
    else:
        conv.status = "active"
        conv.finished_at = None
    session.commit()
    return conv
