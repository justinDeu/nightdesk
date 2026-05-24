from __future__ import annotations

from datetime import datetime, time
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import Ticket


def in_window(start: time, end: time, now: datetime) -> bool:
    # Convention: equal start and end means "always on". The Settings UI
    # writes 00:00 -> 00:00 for the "always on" toggle; any other equal
    # pair (e.g. 13:00 -> 13:00) is treated the same way so a user who
    # types matching times doesn't accidentally pin work to a single
    # minute.
    if start == end:
        return True
    cur = now.time()
    if start < end:
        return start <= cur <= end
    return cur >= start or cur <= end


def _has_unsatisfied_deps(session: Session, ticket: Ticket) -> bool:
    """Return True if the ticket has at least one unsatisfied dependency."""
    from nightdesk.domain.tickets import check_dependencies_satisfied
    satisfied, _ = check_dependencies_satisfied(session, ticket.id)
    return not satisfied


def pick_eligible(
    session: Session,
    *,
    now: datetime,
    window_start: time,
    window_end: time,
    max_parallel: int,
    total_running: int,
) -> List[Ticket]:
    """Pick tickets for this scheduler tick.

    1. Always pick all ``status='queued' AND run_now=true`` tickets, regardless
       of window or capacity. These are user-forced and may push the live count
       above ``max_parallel`` (overflow).
    2. If inside the window, fill remaining ``capacity = max(0, max_parallel -
       total_running)`` slots from ``status='queued' AND run_now=false``,
       ordered by ``(position ASC, priority DESC, created_at ASC)``.

    Tickets with unsatisfied dependencies are skipped in both passes.
    Capacity is clamped at zero; while running > max_parallel, only run-now
    tickets are picked.
    """
    inside_window = in_window(window_start, window_end, now)
    out: list[Ticket] = []

    # Forced run-now picks first, unconditional.
    run_now_stmt = (
        select(Ticket)
        .where(Ticket.status == "queued", Ticket.run_now.is_(True))
        .order_by(Ticket.position.asc(), Ticket.priority.desc(), Ticket.created_at.asc())
    )
    for t in session.scalars(run_now_stmt):
        if t.scheduled_after is not None and t.scheduled_after > now:
            continue
        if _has_unsatisfied_deps(session, t):
            continue
        out.append(t)

    capacity = max(0, max_parallel - total_running)
    if capacity == 0 or not inside_window:
        return out

    normal_stmt = (
        select(Ticket)
        .where(Ticket.status == "queued", Ticket.run_now.is_(False))
        .order_by(Ticket.position.asc(), Ticket.priority.desc(), Ticket.created_at.asc())
    )
    remaining = capacity
    for t in session.scalars(normal_stmt):
        if remaining <= 0:
            break
        if t.scheduled_after is not None and t.scheduled_after > now:
            continue
        if _has_unsatisfied_deps(session, t):
            continue
        out.append(t)
        remaining -= 1
    return out
