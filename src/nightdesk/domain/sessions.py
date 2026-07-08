"""Interactive (ticketless) sessions — a thin façade over ``domain.tickets``.

A *session* is a ``Ticket`` with ``kind='session'``: an ad-hoc, chat-first
agent conversation against a profile and an optional workspace, with no ticket
authoring. It reuses the entire run pipeline unchanged (conversations, run
tokens, workspaces, transcript streaming, run-now dispatch, continue/resume) —
this module only maps the chat verbs onto the existing lifecycle:

    First message  set prompt + request_run_now   draft  -> queued -> running -> review
    Reply          continue_ticket (resume+append) review -> queued -> running -> review
    Resting        (idle)                          review
    Thinking       (transcript streaming)          running

Sessions are hidden from the board, inbox, analytics, search, and saved views
via ``kind='ticket'`` filters at the ticket-list surfaces; see the exclusion
sites in ``domain/tickets.py``, ``domain/query.py``, and ``domain/analytics.py``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import Ticket
from nightdesk.domain.conversations import active_conversation
from nightdesk.domain.tickets import (
    InvalidTransition,
    TicketNotFound,
    _next_position,
    _stage_next_run,
    archive,
    continue_ticket,
    create_ticket,
    get_ticket,
    request_run_now,
)


class SessionBusy(InvalidTransition):
    """Raised when a turn is posted to a session whose current turn is still
    queued or running. v1 rejects concurrent turns (the composer is disabled
    client-side); route handlers map this to HTTP 409."""


_PROMOTE_TARGETS = {"draft", "review"}


def _scratch_root(scratch_root: Optional[Path]) -> Path:
    """Resolve the parent dir under which per-session scratch workspaces live.

    Callers (the API route) pass the app's configured location; the fallback
    resolves it from config so the domain function is usable standalone. The
    directory is a sibling of ``worktree_root`` (e.g.
    ``~/.local/share/nightdesk-sessions``) so ``sandbox.py`` can bind-mount it
    — anything under ``~/.local/share/nightdesk`` is refused by the sandbox.
    """
    if scratch_root is not None:
        return Path(scratch_root)
    from nightdesk.config import load_config

    cfg = load_config()
    return cfg.worktree_root.parent / "nightdesk-sessions"


def create_session(
    session: Session,
    *,
    title: str,
    profile_id: str,
    workspace: Optional[str] = None,
    project_id: Optional[str] = None,
    scratch_root: Optional[Path] = None,
) -> Ticket:
    """Create a ``kind='session'`` ticket in ``draft``.

    ``workspace`` None -> a per-session scratch directory becomes the primary
    (read_write, ``directory``) workspace; a path -> that path is the primary
    directory workspace (the agent works in-place on the live tree).
    """
    if workspace:
        source_path = str(workspace)
    else:
        root = _scratch_root(scratch_root)
        # Provision an empty scratch dir up front. Named later once the ticket
        # id exists would require a second write; instead create under the
        # ticket id we let the DB assign, so mint a placeholder dir keyed by a
        # fresh uuid and reuse it as source_path.
        from nightdesk.db.models import _uuid

        scratch = root / _uuid()
        os.makedirs(scratch, exist_ok=True)
        source_path = str(scratch)

    return create_ticket(
        session,
        kind="session",
        title=title,
        profile_id=profile_id,
        project_id=project_id,
        status="draft",
        workspaces=[{
            "role": "primary",
            "label": "primary",
            "kind": "directory",
            "access": "read_write",
            "source_path": source_path,
            "retention": "preserve",
        }],
    )


def _get_session_ticket(session: Session, session_id: str) -> Ticket:
    t = get_ticket(session, session_id)
    if t.kind != "session":
        raise TicketNotFound(session_id)
    return t


def post_session_turn(session: Session, session_id: str, message: str) -> Ticket:
    """Dispatch one chat turn.

    - No active conversation -> first turn: set ``prompt`` and ``request_run_now``.
    - Active, resumable conversation -> ``continue_ticket`` (resume + append).
    - Active but non-resumable conversation (first turn crashed before a
      session id was recorded) -> retry as a fresh first-run-style turn on a
      NEW conversation instead of 409-ing.
    - Turn already queued/running -> ``SessionBusy`` (409).
    """
    t = _get_session_ticket(session, session_id)
    if t.status in ("queued", "running"):
        raise SessionBusy("a turn is already in flight for this session")

    conv = active_conversation(session, t)
    if conv is not None and conv.session_id:
        # Resumable: append the message on the resumed conversation.
        return continue_ticket(session, session_id, next_run_context=message)

    # First turn (no conversation) or non-resumable conversation: run the
    # message as a fresh prompt. A non-resumable existing conversation forces a
    # brand-new conversation rather than reusing the dead one.
    t.prompt = message
    if conv is not None:
        _stage_next_run(t, intent="retry", new_conversation=True)
    session.commit()
    session.refresh(t)
    return request_run_now(session, session_id)


def promote_session(
    session: Session,
    session_id: str,
    *,
    title: str,
    prompt: Optional[str] = None,
    target_status: str = "review",
) -> Ticket:
    """Promote a session into a real board ticket, keeping its conversations,
    runs, and workspaces.

    Flips ``kind`` to ``'ticket'`` and lands it in ``review`` (so the run
    history stays visible) or ``draft``. Blocked mid-turn (must be review/draft)
    — a running/queued session cannot be promoted.
    """
    if target_status not in _PROMOTE_TARGETS:
        raise InvalidTransition(f"cannot promote session to {target_status!r}")
    t = _get_session_ticket(session, session_id)
    if t.status not in ("review", "draft"):
        raise InvalidTransition(f"cannot promote session from {t.status}")
    t.kind = "ticket"
    t.title = title
    if prompt is not None:
        t.prompt = prompt
    # A kind flip is administrative, not a lifecycle transition: land the row
    # directly in the target board column (and give it a sane board position).
    if t.status != target_status:
        t.status = target_status
        t.position = _next_position(session, target_status)
    session.commit()
    session.refresh(t)
    return t


def list_sessions(session: Session, *, limit: int = 100) -> list[Ticket]:
    """Sessions, most-recently-touched first."""
    stmt = (
        select(Ticket)
        .where(Ticket.kind == "session")
        .order_by(Ticket.updated_at.desc(), Ticket.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def archive_session(session: Session, session_id: str) -> Ticket:
    """Archive a session (review/draft -> archived)."""
    _get_session_ticket(session, session_id)
    return archive(session, session_id)
