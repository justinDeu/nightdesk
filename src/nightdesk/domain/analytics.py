"""Aggregate cost/usage analytics and live-spend helpers.

Per-run ``cost_usd`` and token counts live on the ``Run`` row (written by the
worker via ``domain/cost.py``). This module rolls those up cheaply in SQL for
the ``/analytics`` dashboard and the header spend chip / worker pill.

All cost figures are estimates: the price table in ``domain/cost.py`` is dated
and unknown models contribute ``cost_usd = NULL`` (counted as $0). Callers
should surface "estimate, prices as of <date>" next to any total.

Time bucketing is UTC. ``Run.started_at`` is always present (a run gets a row
the moment it starts), so it drives every window/day boundary. In-flight runs
carry ``cost_usd = NULL`` until they finish, so summing ``cost_usd`` naturally
counts only completed-run spend without an explicit ``finished_at`` filter.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nightdesk.db.models import Profile, Run, Ticket
from nightdesk.domain.cost import PRICES_AS_OF


# --------------------------------------------------------------------------
# Time helpers (UTC).
# --------------------------------------------------------------------------
def start_of_day(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def start_of_month(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


# --------------------------------------------------------------------------
# Spend sums (cheap SQL aggregates).
# --------------------------------------------------------------------------
def spend_between(
    session: Session, *, start: datetime, end: Optional[datetime] = None
) -> float:
    """Sum ``Run.cost_usd`` for runs started in ``[start, end)``.

    NULL costs (unknown model / in-flight) coalesce to 0, so this is the
    completed, priced spend over the window.
    """
    stmt = select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(
        Run.started_at >= start
    )
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    return float(session.scalar(stmt) or 0.0)


# --------------------------------------------------------------------------
# Live spend (day / month).
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SpendStatus:
    day_spend_usd: float
    month_spend_usd: float


def compute_spend_status(session: Session, *, now: datetime) -> SpendStatus:
    """Current day/month completed-run spend (estimate).

    Two cheap SUM queries powering the header spend chip and the worker pill.
    """
    return SpendStatus(
        day_spend_usd=spend_between(session, start=start_of_day(now)),
        month_spend_usd=spend_between(session, start=start_of_month(now)),
    )


# --------------------------------------------------------------------------
# Dashboard aggregations.
# --------------------------------------------------------------------------
def window_totals(
    session: Session, *, start: datetime, end: Optional[datetime] = None
) -> dict:
    """Spend + token totals + run count for runs started in ``[start, end)``."""
    stmt = select(
        func.coalesce(func.sum(Run.cost_usd), 0.0),
        func.coalesce(func.sum(Run.input_tokens), 0),
        func.coalesce(func.sum(Run.output_tokens), 0),
        func.coalesce(func.sum(Run.cache_read_tokens), 0),
        func.coalesce(func.sum(Run.cache_write_tokens), 0),
        func.count(Run.id),
    ).where(Run.started_at >= start)
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    cost, it, ot, cr, cw, n = session.execute(stmt).one()
    it, ot, cr, cw = int(it), int(ot), int(cr), int(cw)
    return {
        "cost": float(cost),
        "input_tokens": it,
        "output_tokens": ot,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "total_tokens": it + ot + cr + cw,
        "run_count": int(n),
    }


def spend_by_profile(
    session: Session, *, start: datetime, end: Optional[datetime] = None
) -> list[dict]:
    """Spend + run count grouped by profile name, highest spend first."""
    stmt = (
        select(
            Profile.name,
            func.coalesce(func.sum(Run.cost_usd), 0.0),
            func.count(Run.id),
        )
        .select_from(Run)
        .join(Ticket, Run.ticket_id == Ticket.id)
        .join(Profile, Ticket.profile_id == Profile.id)
        .where(Run.started_at >= start)
        .group_by(Profile.name)
        .order_by(func.coalesce(func.sum(Run.cost_usd), 0.0).desc())
    )
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    return [
        {"name": name, "cost": float(cost), "run_count": int(n)}
        for name, cost, n in session.execute(stmt).all()
    ]


def spend_by_ticket(
    session: Session,
    *,
    start: datetime,
    end: Optional[datetime] = None,
    limit: int = 10,
) -> list[dict]:
    """Top tickets by spend over the window."""
    stmt = (
        select(
            Ticket.id,
            Ticket.title,
            func.coalesce(func.sum(Run.cost_usd), 0.0),
            func.count(Run.id),
        )
        .select_from(Run)
        .join(Ticket, Run.ticket_id == Ticket.id)
        .where(Run.started_at >= start)
        .group_by(Ticket.id, Ticket.title)
        .order_by(func.coalesce(func.sum(Run.cost_usd), 0.0).desc())
        .limit(limit)
    )
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    return [
        {"ticket_id": tid, "title": title, "cost": float(cost), "run_count": int(n)}
        for tid, title, cost, n in session.execute(stmt).all()
    ]


def run_stats(
    session: Session, *, start: datetime, end: Optional[datetime] = None
) -> dict:
    """Completed-run counts and success rate over the window.

    ``completed`` = runs with a ``finished_at``. ``success`` =
    ``exit_status == 'success'``. Everything else completed (failed,
    cancelled) rolls into ``failure``.
    """
    base = select(func.count(Run.id)).where(
        Run.started_at >= start, Run.finished_at.is_not(None)
    )
    if end is not None:
        base = base.where(Run.started_at < end)
    completed = int(session.scalar(base) or 0)
    success = int(
        session.scalar(base.where(Run.exit_status == "success")) or 0
    )
    failure = completed - success
    success_rate = (success / completed) if completed else 0.0
    return {
        "completed": completed,
        "success": success,
        "failure": failure,
        "success_rate": success_rate,
    }


def duration_percentiles(
    session: Session, *, start: datetime, end: Optional[datetime] = None
) -> dict:
    """Median and p90 run duration (seconds) over completed runs.

    Fetches only the two timestamp columns for completed runs in the window
    and computes percentiles in Python — a bounded set, not "every Run".
    """
    stmt = select(Run.started_at, Run.finished_at).where(
        Run.started_at >= start, Run.finished_at.is_not(None)
    )
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    durations: list[float] = []
    for started, finished in session.execute(stmt).all():
        if started is None or finished is None:
            continue
        secs = (finished - started).total_seconds()
        if secs >= 0:
            durations.append(secs)
    if not durations:
        return {"median_seconds": None, "p90_seconds": None, "count": 0}
    durations.sort()
    median = statistics.median(durations)
    # Nearest-rank p90: index = ceil(0.9 * n) - 1.
    import math

    idx = max(0, math.ceil(0.9 * len(durations)) - 1)
    p90 = durations[idx]
    return {
        "median_seconds": median,
        "p90_seconds": p90,
        "count": len(durations),
    }


def daily_spend_series(
    session: Session, *, start: datetime, now: datetime
) -> list[dict]:
    """Per-day spend from ``start`` through the day containing ``now``.

    Returns one entry per calendar day (UTC), zero-filled, oldest first, so
    the template can render a continuous bar chart.
    """
    rows = session.execute(
        select(
            func.date(Run.started_at),
            func.coalesce(func.sum(Run.cost_usd), 0.0),
        )
        .where(Run.started_at >= start)
        .group_by(func.date(Run.started_at))
    ).all()
    by_day = {str(day): float(cost) for day, cost in rows}

    out: list[dict] = []
    day = start_of_day(start)
    last = start_of_day(now)
    while day <= last:
        key = day.strftime("%Y-%m-%d")
        out.append({"date": key, "cost": by_day.get(key, 0.0)})
        day += timedelta(days=1)
    return out


def build_dashboard(session: Session, *, now: datetime) -> dict:
    """Assemble the full context for the ``/analytics`` page.

    Breakdowns and stats use a rolling 30-day window; the headline chips use
    today / 7-day / 30-day windows.
    """
    today_start = start_of_day(now)
    last_7d_start = today_start - timedelta(days=6)
    last_30d_start = today_start - timedelta(days=29)

    spend = compute_spend_status(session, now=now)
    series = daily_spend_series(session, start=last_30d_start, now=now)
    max_daily = max((d["cost"] for d in series), default=0.0)

    return {
        "prices_as_of": PRICES_AS_OF,
        "today": window_totals(session, start=today_start),
        "last_7d": window_totals(session, start=last_7d_start),
        "last_30d": window_totals(session, start=last_30d_start),
        "by_profile": spend_by_profile(session, start=last_30d_start),
        "by_ticket": spend_by_ticket(session, start=last_30d_start),
        "run_stats": run_stats(session, start=last_30d_start),
        "duration": duration_percentiles(session, start=last_30d_start),
        "daily_series": series,
        "max_daily_cost": max_daily,
        "day_spend_usd": spend.day_spend_usd,
        "month_spend_usd": spend.month_spend_usd,
    }
