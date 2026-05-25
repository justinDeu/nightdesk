from __future__ import annotations

from datetime import datetime, time
from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import ScheduleWindow, Ticket


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


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def window_matches(window: ScheduleWindow, now: datetime) -> bool:
    """True when ``window`` covers the current day-of-week and time.

    ``day_mask`` bit ``1 << now.weekday()`` (Mon=0..Sun=6) must be set, and the
    HH:MM start/end pair must contain ``now`` per ``in_window`` semantics.
    """
    if not (window.day_mask & (1 << now.weekday())):
        return False
    try:
        start = _parse_hhmm(window.start)
        end = _parse_hhmm(window.end)
    except (ValueError, AttributeError):
        return False
    return in_window(start, end, now)


def capacity_for(windows: Sequence[ScheduleWindow], now: datetime) -> Optional[int]:
    """Resolve the parallelism cap from matching windows.

    Returns the highest ``max_parallel`` among windows matching ``now`` (most
    permissive — see notes below). Returns ``None`` when no window matches,
    signalling "no normal dispatch" (capacity 0; run-now still works).

    Overlap policy: most permissive wins (max of the matching caps). If the UX
    should instead be most restrictive, switch ``max`` to ``min`` here and
    revisit. Kept as max per the ticket default.
    """
    matching = [w for w in windows if window_matches(w, now)]
    if not matching:
        return None
    return max(w.max_parallel for w in matching)


def pick_eligible(
    session: Session,
    *,
    now: datetime,
    total_running: int,
) -> List[Ticket]:
    """Pick tickets for this scheduler tick.

    1. Always pick all ``status='queued' AND run_now=true`` tickets, regardless
       of window or capacity. These are user-forced and may push the live count
       above the cap (overflow).
    2. Resolve the capacity from ScheduleWindow rows matching ``now``. With at
       least one matching window, ``max_parallel`` is the highest cap among
       matching windows. With no matching window, capacity is 0 and no normal
       jobs dispatch.
    3. Fill remaining ``capacity = max(0, max_parallel - total_running)`` slots
       from ``status='queued' AND run_now=false``, ordered by
       ``(position ASC, priority DESC, created_at ASC)``.

    Capacity is clamped at zero; while running > max_parallel, only run-now
    tickets are picked.
    """
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
        out.append(t)

    windows = list(
        session.scalars(select(ScheduleWindow).order_by(ScheduleWindow.position.asc()))
    )
    max_parallel = capacity_for(windows, now)
    if max_parallel is None:
        # No window matches the current time/day: no normal dispatch.
        return out

    capacity = max(0, max_parallel - total_running)
    if capacity == 0:
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
        out.append(t)
        remaining -= 1
    return out
