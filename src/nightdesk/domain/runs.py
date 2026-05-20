from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import Run, Ticket


class RunNotFound(Exception):
    pass


def start_run(session: Session, *, ticket_id: str, worktree_path: str,
               transcript_path: str, pid: Optional[int], host: str,
               id: Optional[str] = None,
               started_as_run_now: bool = False,
               intent: str = "first_run",
               parent_run_id: Optional[str] = None,
               headless_policy_version: Optional[str] = None,
               restart_workspace_policy: Optional[str] = None,
               failure_kind: Optional[str] = None) -> Run:
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
    )
    if id is not None:
        kwargs["id"] = id
    r = Run(**kwargs)
    session.add(r)
    session.flush()
    t = session.get(Ticket, ticket_id)
    if t is not None:
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
