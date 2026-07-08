"""Line-anchored review comments on run diffs: CRUD + next-run bundling.

Single-purpose domain module over the ``diff_comments`` table. A *root* comment
(``parent_id IS NULL``) carries the diff anchor and the thread's resolution and
delivery state; a *reply* carries only a body and points at its root. Threads
are one level deep (GitHub-style); a single-user product needs nothing more.

The HTTP layer resolves the anchor from the diff the client is looking at and
passes it in; this module never touches git. ``outdated`` (anchor head vs the
live diff head) is likewise computed by the caller — it depends on a live diff
fetch, not on stored state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import DiffComment, Run, Ticket
from nightdesk.domain.tickets import append_next_run_context


class DiffCommentNotFound(Exception):
    """Raised when a diff comment id does not resolve."""


class InvalidThreadOperation(Exception):
    """Raised for reply-to-reply, resolve-of-reply, or empty request-changes."""


@dataclass(frozen=True)
class Anchor:
    """Where a root comment is pinned on a run's diff."""
    file_path: str
    side: str            # 'old' | 'new'
    line: Optional[int]
    anchor_head_sha: Optional[str]
    anchor_text: Optional[str]


@dataclass(frozen=True)
class Author:
    """The acting principal behind a write (provenance groundwork)."""
    kind: str = "admin"          # 'admin' | 'agent'
    run_id: Optional[str] = None  # the agent's run, when kind == 'agent'


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get(session: Session, comment_id: str) -> DiffComment:
    c = session.get(DiffComment, comment_id)
    if c is None:
        raise DiffCommentNotFound(comment_id)
    return c


def list_run_comments(session: Session, run_id: str) -> list[DiffComment]:
    """All comments for a run (roots and replies), oldest first."""
    return list(session.execute(
        select(DiffComment)
        .where(DiffComment.run_id == run_id)
        .order_by(DiffComment.created_at)
    ).scalars())


def create_comment(session: Session, run_id: str, *, anchor: Anchor,
                   body: str, author: Author) -> DiffComment:
    """Create a root comment anchored to a diff line."""
    body = (body or "").strip()
    if not body:
        raise InvalidThreadOperation("comment body is empty")
    run = session.get(Run, run_id)
    if run is None:
        raise DiffCommentNotFound(run_id)
    c = DiffComment(
        run_id=run_id,
        ticket_id=run.ticket_id,
        conversation_id=run.conversation_id,
        parent_id=None,
        file_path=anchor.file_path,
        side=anchor.side,
        line=anchor.line,
        anchor_head_sha=anchor.anchor_head_sha,
        anchor_text=anchor.anchor_text,
        body=body,
        author_kind=author.kind,
        author_run_id=author.run_id,
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def reply_comment(session: Session, parent_id: str, *, body: str,
                  author: Author) -> DiffComment:
    """Reply to a root comment. Replies to a reply are rejected."""
    body = (body or "").strip()
    if not body:
        raise InvalidThreadOperation("reply body is empty")
    parent = _get(session, parent_id)
    if parent.parent_id is not None:
        raise InvalidThreadOperation("cannot reply to a reply; reply to the root")
    c = DiffComment(
        run_id=parent.run_id,
        ticket_id=parent.ticket_id,
        conversation_id=parent.conversation_id,
        parent_id=parent.id,
        body=body,
        author_kind=author.kind,
        author_run_id=author.run_id,
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def edit_comment(session: Session, comment_id: str, body: str) -> DiffComment:
    body = (body or "").strip()
    if not body:
        raise InvalidThreadOperation("comment body is empty")
    c = _get(session, comment_id)
    c.body = body
    session.commit()
    session.refresh(c)
    return c


def set_resolved(session: Session, comment_id: str, resolved: bool,
                 author: Author) -> DiffComment:
    """Resolve/reopen a root thread. Resolving a reply is rejected."""
    c = _get(session, comment_id)
    if c.parent_id is not None:
        raise InvalidThreadOperation("only a root thread can be resolved")
    c.resolved = resolved
    c.resolved_at = _now() if resolved else None
    session.commit()
    session.refresh(c)
    return c


def delete_comment(session: Session, comment_id: str) -> None:
    """Delete a comment. Deleting a root cascades its replies."""
    c = _get(session, comment_id)
    session.delete(c)
    session.commit()


def unresolved_threads(session: Session, run_id: str) -> list[DiffComment]:
    """Root threads for a run that are not resolved, oldest first."""
    return list(session.execute(
        select(DiffComment)
        .where(
            DiffComment.run_id == run_id,
            DiffComment.parent_id.is_(None),
            DiffComment.resolved.is_(False),
        )
        .order_by(DiffComment.created_at)
    ).scalars())


def _format_anchor(root: DiffComment) -> str:
    """Human/agent-readable anchor label for the bundled block."""
    path = root.file_path or "(unknown file)"
    side = root.side or "new"
    if root.line:
        return f"{path}:{root.line} ({side})"
    return f"{path} ({side})"


def _format_block(roots: list[DiffComment]) -> str:
    """Render unresolved roots + replies as a plain, diff-anchored block.

    Not a chat log — a structured, agent-readable list the next run folds into
    its prompt via ``next_run_context``.
    """
    n = len(roots)
    lines = [f"## Review comments to address ({n} unresolved)"]
    for root in roots:
        lines.append(f"- {_format_anchor(root)}: \"{root.body.strip()}\"")
        for reply in root.replies:
            who = reply.author_kind
            lines.append(f"    ↳ {who}: {reply.body.strip()}")
    return "\n".join(lines)


def request_changes(session: Session, run_id: str) -> Ticket:
    """Bundle the run's unresolved threads into the ticket's next-run-context.

    Formats every unresolved root (+ its replies) into one structured block,
    appends it to ``ticket.next_run_context`` (reusing ``append_next_run_context``
    so guidance stacks rather than overwrites), and stamps ``delivered_at`` on
    each delivered root. Refuses when there is nothing unresolved (mirrors the
    empty-guard on ``merge_next_run_context_into_prompt``).
    """
    roots = unresolved_threads(session, run_id)
    if not roots:
        raise InvalidThreadOperation("no unresolved review comments to send")
    ticket_id = roots[0].ticket_id
    block = _format_block(roots)
    ticket = append_next_run_context(session, ticket_id, block)
    stamp = _now()
    for root in roots:
        root.delivered_at = stamp
    session.commit()
    session.refresh(ticket)
    return ticket
