from __future__ import annotations

from datetime import datetime, time
from typing import List, Optional, Sequence
from zoneinfo import ZoneInfo

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


def _has_unsatisfied_deps(session: Session, ticket: Ticket) -> bool:
    """Return True if the ticket has at least one unsatisfied dependency."""
    from nightdesk.domain.tickets import check_dependencies_satisfied
    satisfied, _ = check_dependencies_satisfied(session, ticket.id)
    return not satisfied


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def window_matches(window: ScheduleWindow, now: datetime, tz: ZoneInfo) -> bool:
    """True when ``window`` covers the current day-of-week and time in ``tz``.

    ``now`` is an aware UTC instant; it is converted into ``tz`` first so the
    weekday bit and the HH:MM range are both evaluated against local wall-clock
    time. ``day_mask`` bit ``1 << local.weekday()`` (Mon=0..Sun=6) must be set.
    """
    local = now.astimezone(tz)
    if not (window.day_mask & (1 << local.weekday())):
        return False
    try:
        start = _parse_hhmm(window.start)
        end = _parse_hhmm(window.end)
    except (ValueError, AttributeError):
        return False
    return in_window(start, end, local)


def capacity_for(windows: Sequence[ScheduleWindow], now: datetime, tz: ZoneInfo) -> Optional[int]:
    """Resolve the parallelism cap from windows matching ``now`` in ``tz``.

    When several windows overlap the same instant, precedence follows the
    user-defined order: ``windows`` must be supplied sorted by ``position``
    ascending, and the first (lowest-position) matching window wins. This is
    the order the Settings editor exposes via drag-to-reorder, so dragging a
    window above another makes its cap take precedence on overlap. Returns
    ``None`` when no window matches (capacity 0; run-now still works).
    """
    for w in windows:
        if window_matches(w, now, tz):
            return w.max_parallel
    return None


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
       least one matching window, ``max_parallel`` is the cap of the
       highest-precedence matching window (the first in ``position`` order, i.e.
       the one dragged highest in the Settings editor). With no matching window,
       capacity is 0 and no normal jobs dispatch.
    3. Fill remaining ``capacity = max(0, max_parallel - total_running)`` slots
       from ``status='queued' AND run_now=false``. Queue position is the primary
       run order; priority is only a tie-breaker for equal positions:
       ``(position ASC, priority DESC, created_at ASC)``.

    Tickets with unsatisfied dependencies are skipped in both passes.
    Capacity is clamped at zero; while running > max_parallel, only run-now
    tickets are picked.
    """
    out: list[Ticket] = []

    # Forced run-now picks first, unconditional (ignores window).
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

    from nightdesk.db.models import ConfigRow
    cfg = session.get(ConfigRow, 1)
    tz_name = cfg.schedule_timezone if cfg and cfg.schedule_timezone else "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    windows = list(
        session.scalars(
            select(ScheduleWindow).order_by(
                ScheduleWindow.position.asc(), ScheduleWindow.id.asc()
            )
        )
    )
    max_parallel = capacity_for(windows, now, tz)
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
        if _has_unsatisfied_deps(session, t):
            continue
        out.append(t)
        remaining -= 1
    return out
