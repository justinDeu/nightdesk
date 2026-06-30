from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import Run, Ticket


class RunNotFound(Exception):
    pass


def _ensure_conversation_for_run(
    session: Session, ticket: Ticket, run: Run,
    *, conversation_id: Optional[str], transcript_path: str,
) -> None:
    """Bind ``run`` to a conversation, creating one if the ticket has none.

    Resolution order: an explicit ``conversation_id`` (the Continue path);
    then the ticket's active conversation (another turn in it); otherwise a
    brand-new conversation (the first turn, or a New-conversation run). The
    active conversation pointer is (re)set so the just-started turn's
    conversation is current.
    """
    from nightdesk.db.models import Conversation
    from nightdesk.domain.conversations import (
        ConversationNotFound, create_conversation, get_conversation,
        next_turn_position,
    )

    if conversation_id is not None:
        try:
            conv = get_conversation(session, conversation_id)
        except ConversationNotFound:
            conv = None
    elif ticket.current_conversation_id:
        conv = session.get(Conversation, ticket.current_conversation_id)
    else:
        conv = None

    if conv is None:
        profile = ticket.profile
        backend = (getattr(profile, "backend", None) or "claude_sdk") if profile else "claude_sdk"
        conv = create_conversation(
            session,
            ticket_id=ticket.id,
            profile_id=ticket.profile_id,
            backend=backend,
            transcript_path=transcript_path,
        )

    # Compute position BEFORE assigning conversation_id: otherwise SQLAlchemy's
    # autoflush would include this very run in the "max(position)" query and
    # off-by-one the first turn to position 1.
    run.position = next_turn_position(session, conv.id)
    run.conversation_id = conv.id
    if ticket.current_conversation_id != conv.id:
        ticket.current_conversation_id = conv.id


def start_run(session: Session, *, ticket_id: str, worktree_path: str,
               transcript_path: str, pid: Optional[int], host: str,
               id: Optional[str] = None,
               started_as_run_now: bool = False,
               intent: str = "first_run",
               parent_run_id: Optional[str] = None,
               conversation_id: Optional[str] = None,
               headless_policy_version: Optional[str] = None,
               restart_workspace_policy: Optional[str] = None,
               failure_kind: Optional[str] = None,
               prompt: Optional[str] = None,
               sandbox_tool_paths: Optional[list[str]] = None) -> Run:
    kwargs = dict(
        ticket_id=ticket_id,
        started_at=datetime.now(timezone.utc),
        worktree_path=worktree_path,
        transcript_path=transcript_path,
        pid=pid,
        host=host,
        started_as_run_now=started_as_run_now,
        intent=intent,
        parent_run_id=parent_run_id,
        headless_policy_version=headless_policy_version,
        restart_workspace_policy=restart_workspace_policy,
        failure_kind=failure_kind,
        prompt=prompt,
        sandbox_tool_paths=sandbox_tool_paths,
    )
    if id is not None:
        kwargs["id"] = id
    r = Run(**kwargs)
    session.add(r)
    session.flush()
    t = session.get(Ticket, ticket_id)
    if t is not None:
        _ensure_conversation_for_run(
            session, t, r, conversation_id=conversation_id, transcript_path=transcript_path,
        )
        t.current_run_id = r.id
    session.commit()
    session.refresh(r)
    return r


def finish_run(session: Session, run_id: str, *, exit_status: str,
                error_summary: Optional[str],
                session_id: Optional[str] = None) -> Run:
    r = session.get(Run, run_id)
    if r is None:
        raise RunNotFound(run_id)
    r.finished_at = datetime.now(timezone.utc)
    r.exit_status = exit_status
    r.error_summary = error_summary
    # Only set when the SDK reported one; don't clobber an existing id with None.
    if session_id:
        r.session_id = session_id
    session.commit()
    # Lift the authoritative session_id + cumulative totals onto the
    # conversation. Per the cost rule the conversation totals equal the LAST
    # turn's reported totals, not a sum — so re-syncing on each finish (and
    # again after the worker persists usage) keeps the conversation current.
    if r.conversation_id is not None:
        from nightdesk.domain.conversations import sync_conversation_from_turn
        sync_conversation_from_turn(session, r)
    session.refresh(r)
    return r


def list_runs(session: Session, ticket_id: Optional[str] = None) -> list[Run]:
    stmt = select(Run).order_by(Run.started_at.desc())
    if ticket_id is not None:
        stmt = stmt.where(Run.ticket_id == ticket_id)
    return list(session.scalars(stmt))


def get_run(session: Session, run_id: str) -> Run:
    r = session.get(Run, run_id)
    if r is None:
        raise RunNotFound(run_id)
    return r
