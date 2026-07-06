"""Aggregate cost/usage analytics and live-spend helpers.

Per-run ``cost_usd`` and token counts live on the ``Run`` row (written by the
worker via ``domain/cost.py``). This module rolls those up cheaply in SQL for
the ``/analytics`` dashboard and the header spend chip / worker pill.

All cost figures are estimates: prices are resolved live → cached → bundled
(see ``domain/pricing.py``) and unknown models contribute $0. Callers should
surface the price source and its as-of date next to any total.

Time bucketing is timezone-aware. Day/week/month boundaries are localized to a
caller-supplied zone (defaulting to UTC, which reproduces the original behavior)
and then expressed as aware UTC instants: ``Run.started_at`` is always stored in
UTC, so the query side stays UTC and only the boundary localization shifts.
``Run.started_at`` is always present (a run gets a row the moment it starts), so
it drives every window/day boundary. In-flight runs
carry ``cost_usd = NULL`` until they finish, so summing ``cost_usd`` naturally
counts only completed-run spend without an explicit ``finished_at`` filter.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from nightdesk.db.models import Profile, Project, Run, RunLatency, Ticket
from nightdesk.domain import pricing
from nightdesk.domain.pricing import PriceInfo


# --------------------------------------------------------------------------
# Time helpers (timezone-aware).
#
# Day/month boundaries are computed in a caller-supplied zone (``tz``) and
# returned as aware UTC instants. ``Run.started_at`` is stored in UTC, so the
# query side keeps comparing in UTC; only the *boundary localization* changes.
# Defaulting ``tz`` to UTC reproduces the original behavior exactly.
# --------------------------------------------------------------------------
_UTC = ZoneInfo("UTC")


def start_of_day(now: datetime, tz: ZoneInfo = _UTC) -> datetime:
    """Local midnight of ``now`` in ``tz``, returned as an aware UTC instant.

    Default ``tz=UTC`` gives UTC midnight (the original behavior). For a user in
    a negative-UTC zone, local midnight lands the *previous* UTC day, which is
    exactly what stops local-morning work from aging off early as UTC rolls
    over.
    """
    local = now.astimezone(tz)
    return local.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)


def start_of_month(now: datetime, tz: ZoneInfo = _UTC) -> datetime:
    """Midnight of local-day-1 of ``now`` in ``tz``, as an aware UTC instant."""
    local = now.astimezone(tz)
    return local.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)


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


def compute_spend_status(
    session: Session, *, now: datetime, tz: ZoneInfo = _UTC
) -> SpendStatus:
    """Current day/month completed-run spend (estimate).

    Two cheap SUM queries powering the header spend chip and the worker pill.
    ``tz`` localizes the day/month boundaries (default UTC = original behavior).
    """
    return SpendStatus(
        day_spend_usd=spend_between(session, start=start_of_day(now, tz)),
        month_spend_usd=spend_between(session, start=start_of_month(now, tz)),
    )


# --------------------------------------------------------------------------
# Dashboard aggregations.
# --------------------------------------------------------------------------
def cache_hit_rate(
    input_tokens: int, cache_read_tokens: int, cache_write_tokens: int
) -> float:
    """Share of prompt-side tokens served from cache (0..1).

    Prompt tokens = fresh input + cache writes (first-time priming) + cache
    reads. Output tokens are excluded since they're generated, never cached.
    A high rate means most of the context was reused cheaply.
    """
    prompt = input_tokens + cache_read_tokens + cache_write_tokens
    return (cache_read_tokens / prompt) if prompt else 0.0


# A run with a stamped ``pricing_snapshot`` prices from that snapshot's
# stored ``cost_usd`` alone, never re-derived from current prices (the point
# of the snapshot). Runs with no snapshot (everything pre-merge, or a launch
# where snapshot-building failed) keep the legacy repricing path and are
# flagged "estimated" so the UI can label them "estimated at current prices".
_HAS_SNAPSHOT = case((Run.pricing_snapshot.is_not(None), 1), else_=0)


def _split_cost(
    model: Optional[str], it: int, ot: int, cr: int, cw: int,
    has_snapshot: bool, stored_cost: float, prices: Optional[PriceInfo],
) -> tuple[float, bool]:
    """Cost contribution for one ``(model, has_snapshot)`` grouped row.

    Snapshotted rows always use the stored (historical) cost. Non-snapshot
    rows reprice from ``prices`` when repricing is requested, else also fall
    back to the stored cost (the ``prices=None`` direct-unit-test path, whose
    behavior predates snapshots and stays unchanged). Returns
    ``(cost, estimated)``.
    """
    if has_snapshot:
        return float(stored_cost), False
    if prices is not None:
        return prices.cost(model, it, ot, cr, cw), True
    return float(stored_cost), False


# Token-sum columns reused by several aggregates, in a stable order.
_TOKEN_SUMS = (
    func.coalesce(func.sum(Run.input_tokens), 0),
    func.coalesce(func.sum(Run.output_tokens), 0),
    func.coalesce(func.sum(Run.cache_read_tokens), 0),
    func.coalesce(func.sum(Run.cache_write_tokens), 0),
)
_TOTAL_TOKENS = (
    func.coalesce(func.sum(Run.input_tokens), 0)
    + func.coalesce(func.sum(Run.output_tokens), 0)
    + func.coalesce(func.sum(Run.cache_read_tokens), 0)
    + func.coalesce(func.sum(Run.cache_write_tokens), 0)
)


def _token_row(it, ot, cr, cw, n, cost) -> dict:
    it, ot, cr, cw = int(it), int(ot), int(cr), int(cw)
    return {
        "input_tokens": it,
        "output_tokens": ot,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "total_tokens": it + ot + cr + cw,
        "cache_hit_rate": cache_hit_rate(it, cr, cw),
        "run_count": int(n),
        "cost": float(cost),
    }


def _scope_to_project(stmt, *, project_id: Optional[str] = None):
    """Join ``Ticket`` and filter to one project, when ``project_id`` is given.

    Shared by every ``Run``-rooted aggregate below so ``project_id`` is an
    optional, additive filter everywhere: omit it and behavior is unchanged.
    """
    if project_id is None:
        return stmt
    return stmt.join(Ticket, Run.ticket_id == Ticket.id).where(
        Ticket.project_id == project_id
    )


def _window_cost_and_estimated(
    session: Session,
    prices: PriceInfo,
    *,
    start: datetime,
    end: Optional[datetime] = None,
    project_id: Optional[str] = None,
) -> tuple[float, int]:
    """Reprice ``[start, end)``: stored cost for snapshotted runs, repriced
    from ``prices`` for the rest. Returns ``(cost, estimated_run_count)``."""
    stmt = (
        select(Run.model_used, _HAS_SNAPSHOT, *_TOKEN_SUMS,
               func.count(Run.id), func.coalesce(func.sum(Run.cost_usd), 0.0))
        .where(Run.started_at >= start)
    )
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    stmt = _scope_to_project(stmt, project_id=project_id)
    stmt = stmt.group_by(Run.model_used, _HAS_SNAPSHOT)
    total = 0.0
    estimated = 0
    for model, has_snap, it, ot, cr, cw, n, cost in session.execute(stmt).all():
        c, is_est = _split_cost(model, int(it), int(ot), int(cr), int(cw),
                                 bool(has_snap), float(cost), prices)
        total += c
        if is_est:
            estimated += int(n)
    return total, estimated


def window_totals(
    session: Session, *, start: datetime, end: Optional[datetime] = None,
    prices: Optional[PriceInfo] = None, project_id: Optional[str] = None,
) -> dict:
    """Token totals + cache-hit rate + run count + cost for ``[start, end)``.

    With ``prices`` given the cost is snapshot-stored where a run has one,
    repriced from live/cached prices otherwise (see ``_split_cost``); without
    ``prices`` the stored ``cost_usd`` sum is returned (the original behavior,
    used by direct unit tests). ``project_id`` scopes to runs on that
    project's tickets (omit for the global totals). The returned dict adds
    ``estimated_runs``: the count of runs in the window priced by repricing
    rather than a stamped snapshot, so the UI can label the total accordingly.
    """
    stmt = select(
        *_TOKEN_SUMS,
        func.count(Run.id),
        func.coalesce(func.sum(Run.cost_usd), 0.0),
        func.coalesce(func.sum(case((Run.pricing_snapshot.is_(None), 1), else_=0)), 0),
    ).where(Run.started_at >= start)
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    stmt = _scope_to_project(stmt, project_id=project_id)
    it, ot, cr, cw, n, cost, no_snapshot_n = session.execute(stmt).one()
    estimated_runs = int(no_snapshot_n)
    if prices is not None:
        cost, estimated_runs = _window_cost_and_estimated(
            session, prices, start=start, end=end, project_id=project_id,
        )
    row = _token_row(it, ot, cr, cw, n, cost)
    row["estimated_runs"] = estimated_runs
    return row


def tokens_by_model(
    session: Session, *, start: datetime, end: Optional[datetime] = None,
    prices: Optional[PriceInfo] = None, project_id: Optional[str] = None,
) -> list[dict]:
    """Token totals + cache-hit rate grouped by model, most tokens first.

    Runs with no recorded ``model_used`` group under ``"unknown"``. Each row
    carries ``estimated_runs`` (count of that model's runs priced by
    repricing rather than a stamped snapshot) and ``estimated`` (whether any
    are), additive fields for the "estimated at current prices" UI label.
    """
    stmt = (
        select(
            Run.model_used,
            _HAS_SNAPSHOT,
            *_TOKEN_SUMS,
            func.count(Run.id),
            func.coalesce(func.sum(Run.cost_usd), 0.0),
        )
        .where(Run.started_at >= start)
        .group_by(Run.model_used, _HAS_SNAPSHOT)
    )
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    stmt = _scope_to_project(stmt, project_id=project_id)
    agg: dict[str, dict] = {}
    for model, has_snap, it, ot, cr, cw, n, cost in session.execute(stmt).all():
        key = model or "unknown"
        it, ot, cr, cw, n = int(it), int(ot), int(cr), int(cw), int(n)
        slot = agg.setdefault(key, {"it": 0, "ot": 0, "cr": 0, "cw": 0, "n": 0,
                                     "cost": 0.0, "estimated_runs": 0})
        c, is_est = _split_cost(model, it, ot, cr, cw, bool(has_snap), float(cost), prices)
        slot["it"] += it
        slot["ot"] += ot
        slot["cr"] += cr
        slot["cw"] += cw
        slot["n"] += n
        slot["cost"] += c
        if is_est:
            slot["estimated_runs"] += n
    out = []
    for model, a in agg.items():
        row = {"model": model, **_token_row(a["it"], a["ot"], a["cr"], a["cw"], a["n"], a["cost"])}
        row["estimated_runs"] = a["estimated_runs"]
        row["estimated"] = a["estimated_runs"] > 0
        out.append(row)
    out.sort(key=lambda r: r["total_tokens"], reverse=True)
    return out


def usage_by_profile(
    session: Session, *, start: datetime, end: Optional[datetime] = None,
    prices: Optional[PriceInfo] = None, project_id: Optional[str] = None,
) -> list[dict]:
    """Token totals + run count grouped by profile name, most tokens first.

    Groups by ``(profile, model, has_snapshot)`` so the cost can be repriced
    from resolved prices for non-snapshot runs while snapshotted runs keep
    their stored cost; the rows are rolled back up to one per profile. Each
    row carries ``estimated_runs``/``estimated`` (see ``tokens_by_model``).
    """
    stmt = (
        select(
            Profile.name,
            Run.model_used,
            _HAS_SNAPSHOT,
            *_TOKEN_SUMS,
            func.count(Run.id),
            func.coalesce(func.sum(Run.cost_usd), 0.0),
        )
        .select_from(Run)
        .join(Ticket, Run.ticket_id == Ticket.id)
        .join(Profile, Ticket.profile_id == Profile.id)
        .where(Run.started_at >= start)
        .group_by(Profile.name, Run.model_used, _HAS_SNAPSHOT)
    )
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    if project_id is not None:
        stmt = stmt.where(Ticket.project_id == project_id)
    agg: dict[str, dict] = {}
    for name, model, has_snap, it, ot, cr, cw, n, cost in session.execute(stmt).all():
        slot = agg.setdefault(name, {"it": 0, "ot": 0, "cr": 0, "cw": 0, "n": 0,
                                      "cost": 0.0, "estimated_runs": 0})
        it, ot, cr, cw, n = int(it), int(ot), int(cr), int(cw), int(n)
        c, is_est = _split_cost(model, it, ot, cr, cw, bool(has_snap), float(cost), prices)
        slot["it"] += it
        slot["ot"] += ot
        slot["cr"] += cr
        slot["cw"] += cw
        slot["n"] += n
        slot["cost"] += c
        if is_est:
            slot["estimated_runs"] += n
    rows = [{"name": name, **_token_row(a["it"], a["ot"], a["cr"], a["cw"], a["n"], a["cost"]),
             "estimated_runs": a["estimated_runs"], "estimated": a["estimated_runs"] > 0}
            for name, a in agg.items()]
    rows.sort(key=lambda r: r["total_tokens"], reverse=True)
    return rows


def usage_by_ticket(
    session: Session,
    *,
    start: datetime,
    end: Optional[datetime] = None,
    limit: int = 10,
    prices: Optional[PriceInfo] = None,
    project_id: Optional[str] = None,
) -> list[dict]:
    """Top tickets by total tokens over the window.

    Groups by ``(ticket, model, has_snapshot)`` so the cost can be repriced
    from resolved prices for non-snapshot runs while snapshotted runs keep
    their stored cost; rows are rolled back up to one per ticket and
    truncated to ``limit``. Each row carries ``estimated_runs``/``estimated``
    (see ``tokens_by_model``).
    """
    stmt = (
        select(
            Ticket.id,
            Ticket.title,
            Run.model_used,
            _HAS_SNAPSHOT,
            *_TOKEN_SUMS,
            func.count(Run.id),
            func.coalesce(func.sum(Run.cost_usd), 0.0),
        )
        .select_from(Run)
        .join(Ticket, Run.ticket_id == Ticket.id)
        .where(Run.started_at >= start)
        .group_by(Ticket.id, Ticket.title, Run.model_used, _HAS_SNAPSHOT)
    )
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    if project_id is not None:
        stmt = stmt.where(Ticket.project_id == project_id)
    agg: dict[str, dict] = {}
    titles: dict[str, str] = {}
    for tid, title, model, has_snap, it, ot, cr, cw, n, cost in session.execute(stmt).all():
        titles[tid] = title
        slot = agg.setdefault(tid, {"it": 0, "ot": 0, "cr": 0, "cw": 0, "n": 0,
                                     "cost": 0.0, "estimated_runs": 0})
        it, ot, cr, cw, n = int(it), int(ot), int(cr), int(cw), int(n)
        c, is_est = _split_cost(model, it, ot, cr, cw, bool(has_snap), float(cost), prices)
        slot["it"] += it
        slot["ot"] += ot
        slot["cr"] += cr
        slot["cw"] += cw
        slot["n"] += n
        slot["cost"] += c
        if is_est:
            slot["estimated_runs"] += n
    rows = [{"ticket_id": tid, "title": titles[tid],
             **_token_row(a["it"], a["ot"], a["cr"], a["cw"], a["n"], a["cost"]),
             "estimated_runs": a["estimated_runs"], "estimated": a["estimated_runs"] > 0}
            for tid, a in agg.items()]
    rows.sort(key=lambda r: r["total_tokens"], reverse=True)
    return rows[:limit]


def run_stats(
    session: Session, *, start: datetime, end: Optional[datetime] = None,
    project_id: Optional[str] = None,
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
    base = _scope_to_project(base, project_id=project_id)
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


def _nearest_rank(sorted_values: list[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile over an already-sorted, non-empty list.

    Index = ceil(pct * n) - 1, clamped to [0, n-1]. Matches the original
    ``duration_percentiles`` p90 math.
    """
    if not sorted_values:
        return None
    idx = max(0, math.ceil(pct * len(sorted_values)) - 1)
    return sorted_values[min(idx, len(sorted_values) - 1)]


def _percentiles(values: list[float]) -> dict:
    """Median / p90 / p95 / p99 / count over a sample list (nearest-rank p90/p95/p99)."""
    if not values:
        return {"median_seconds": None, "p90_seconds": None,
                "p95_seconds": None,
                "p99_seconds": None, "count": 0}
    ordered = sorted(values)
    return {
        "median_seconds": statistics.median(ordered),
        "p90_seconds": _nearest_rank(ordered, 0.90),
        "p95_seconds": _nearest_rank(ordered, 0.95),
        "p99_seconds": _nearest_rank(ordered, 0.99),
        "count": len(ordered),
    }


def duration_percentiles(
    session: Session, *, start: datetime, end: Optional[datetime] = None,
    project_id: Optional[str] = None,
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
    stmt = _scope_to_project(stmt, project_id=project_id)
    durations: list[float] = []
    for started, finished in session.execute(stmt).all():
        if started is None or finished is None:
            continue
        secs = (finished - started).total_seconds()
        if secs >= 0:
            durations.append(secs)
    if not durations:
        return {"median_seconds": None, "p90_seconds": None, "count": 0}
    ordered = sorted(durations)
    return {
        "median_seconds": statistics.median(ordered),
        "p90_seconds": _nearest_rank(ordered, 0.90),
        "count": len(ordered),
    }


# Stable, dark-background-friendly palette assigned to models by token rank.
# Models beyond the palette length cycle back through it.
MODEL_PALETTE = [
    "#6ea8fe",  # blue
    "#34d399",  # green
    "#fbbf24",  # amber
    "#a78bfa",  # violet
    "#f472b6",  # pink
    "#22d3ee",  # cyan
    "#fb7185",  # rose
    "#94a3b8",  # slate (fallback)
]


def daily_usage_by_model_series(
    session: Session, *, start: datetime, now: datetime,
    prices: Optional[PriceInfo] = None,
    tz: ZoneInfo = _UTC,
    project_id: Optional[str] = None,
) -> list[dict]:
    """Per-day token/spend/run-count totals from ``start`` to ``now``'s day.

    Returns one entry per local calendar day (in ``tz``; default UTC), zero-
    filled, oldest first::

        {date, total_tokens, input_tokens, output_tokens, cache_read_tokens,
         cache_write_tokens, cost, run_count, by_model: {model: tokens}}

    Runs with no recorded model group under ``"unknown"``. Cost is repriced from
    ``prices`` when given, else summed from stored ``cost_usd``. ``project_id``
    scopes to one project's tickets (omit for the global series).

    Both the SQL bucketing and the day labels are aligned to ``tz``: the stored
    UTC ``started_at`` is shifted by the local UTC offset before ``func.date``
    extracts the day, and the emitted labels are local dates. This keeps a run
    attributed to the day the user actually experienced it (a local-evening run
    for a negative-offset user otherwise lands on the *next* UTC date). The
    offset is taken at ``now``; the bounded 30-day window means a DST
    transition can mis-attribute at most the single transition day.
    """
    # Shift the stored UTC instant by the local UTC offset so func.date() yields
    # the local calendar date. '+0 seconds' (UTC) is a no-op.
    offset = now.astimezone(tz).utcoffset() or timedelta(0)
    day_mod = f"{int(offset.total_seconds()):+d} seconds"
    day_expr = func.date(Run.started_at, day_mod)

    stmt = (
        select(
            day_expr,
            Run.model_used,
            _HAS_SNAPSHOT,
            *_TOKEN_SUMS,
            func.count(Run.id),
            func.coalesce(func.sum(Run.cost_usd), 0.0),
        )
        .where(Run.started_at >= start)
        .group_by(day_expr, Run.model_used, _HAS_SNAPSHOT)
    )
    stmt = _scope_to_project(stmt, project_id=project_id)
    rows = session.execute(stmt).all()

    by_day: dict[str, dict] = {}
    for day, model, has_snap, it, ot, cr, cw, n, cost in rows:
        it, ot, cr, cw, n = int(it), int(ot), int(cr), int(cw), int(n)
        tokens = it + ot + cr + cw
        key = str(day)
        slot = by_day.setdefault(key, {
            "total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "cost": 0.0, "run_count": 0, "by_model": {}, "estimated_runs": 0,
        })
        slot["by_model"][model or "unknown"] = slot["by_model"].get(model or "unknown", 0) + tokens
        slot["total_tokens"] += tokens
        slot["input_tokens"] += it
        slot["output_tokens"] += ot
        slot["cache_read_tokens"] += cr
        slot["cache_write_tokens"] += cw
        slot["run_count"] += n
        c, is_est = _split_cost(model, it, ot, cr, cw, bool(has_snap), float(cost), prices)
        slot["cost"] += c
        if is_est:
            slot["estimated_runs"] += n

    out: list[dict] = []
    # Iterate over local dates (start_of_day already localizes to tz); strftime
    # the local date so labels match the shifted SQL buckets.
    day = start_of_day(start, tz).astimezone(tz)
    last = start_of_day(now, tz).astimezone(tz)
    while day <= last:
        key = day.strftime("%Y-%m-%d")
        slot = by_day.get(key, {
            "total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "cost": 0.0, "run_count": 0, "by_model": {}, "estimated_runs": 0,
        })
        out.append({"date": key, **slot})
        day += timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# Latency aggregations (read the cached ``run_latency`` rows, never files).
#
# Per-run latency summaries are derived from transcripts once, at run
# completion, and cached on ``run_latency`` (see ``domain.latency``). These
# rollups aggregate those rows over the window. Models come from
# ``RunLatency.model`` (== the run's ``model_used``); runs with no model group
# under ``"unknown"`` to mirror ``tokens_by_model``.
# --------------------------------------------------------------------------
def _latency_window_stmt(columns, session: Session, *, start: datetime,
                         end: Optional[datetime], project_id: Optional[str] = None):
    """A RunLatency⋈Run select scoped to runs started in [start, end)."""
    stmt = (
        select(*columns)
        .select_from(RunLatency)
        .join(Run, RunLatency.run_id == Run.id)
        .where(Run.started_at >= start)
    )
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    if project_id is not None:
        stmt = stmt.join(Ticket, Run.ticket_id == Ticket.id).where(
            Ticket.project_id == project_id
        )
    return stmt


def _as_floats(value) -> list[float]:
    """Coerce a JSON ``turn_latencies`` cell into a list of floats."""
    if not isinstance(value, list):
        return []
    return [float(x) for x in value if isinstance(x, (int, float))]


def latency_by_model(
    session: Session, *, start: datetime, end: Optional[datetime] = None,
    project_id: Optional[str] = None,
) -> list[dict]:
    """Median / p90 / p99 per-turn latency + sample count, grouped by model.

    Operates over per-turn samples (``run_latency.turn_latencies`` merged across
    runs), so ``count`` is the number of turns, not runs. Most samples first,
    like ``tokens_by_model``.
    """
    stmt = _latency_window_stmt(
        (RunLatency.model, RunLatency.turn_latencies), session,
        start=start, end=end, project_id=project_id,
    )
    by_model: dict[str, list[float]] = {}
    for model, lats in session.execute(stmt).all():
        by_model.setdefault(model or "unknown", []).extend(_as_floats(lats))
    rows = [{"model": m, **_percentiles(lats)} for m, lats in by_model.items()]
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


def model_vs_tool_time(
    session: Session, *, start: datetime, end: Optional[datetime] = None,
    project_id: Optional[str] = None,
) -> list[dict]:
    """Per-model totals of model-inference time vs tool-execution time.

    ``model_seconds`` = sum of per-turn latencies; ``tool_seconds`` = sum of
    tool-execution gaps. Most total time first.
    """
    stmt = _latency_window_stmt(
        (
            RunLatency.model,
            func.coalesce(func.sum(RunLatency.total_model_seconds), 0.0),
            func.coalesce(func.sum(RunLatency.total_tool_seconds), 0.0),
            func.count(RunLatency.run_id),
        ),
        session, start=start, end=end, project_id=project_id,
    ).group_by(RunLatency.model)
    rows = []
    for model, ms, ts, n in session.execute(stmt).all():
        rows.append({
            "model": model or "unknown",
            "model_seconds": float(ms),
            "tool_seconds": float(ts),
            "run_count": int(n),
        })
    rows.sort(key=lambda r: r["model_seconds"] + r["tool_seconds"], reverse=True)
    return rows


def daily_latency_by_model_series(
    session: Session, *, start: datetime, now: datetime,
    tz: ZoneInfo = _UTC,
    project_id: Optional[str] = None,
) -> list[dict]:
    """Per-day per-model MEDIAN per-turn latency from ``start`` to ``now``'s day.

    Structured like ``daily_usage_by_model_series`` (one entry per local
    calendar day, zero-filled, oldest first) so the existing-chart style can
    render it: ``{date, by_model: {model: median_seconds}}``. Days/models with
    no turn samples have empty/absent entries. Day bucketing is aligned to
    ``tz`` exactly as in ``daily_usage_by_model_series``. ``project_id`` scopes
    to one project's tickets (omit for the global series).
    """
    offset = now.astimezone(tz).utcoffset() or timedelta(0)
    day_mod = f"{int(offset.total_seconds()):+d} seconds"
    day_expr = func.date(Run.started_at, day_mod)

    stmt = (
        select(day_expr, RunLatency.model, RunLatency.turn_latencies)
        .select_from(RunLatency)
        .join(Run, RunLatency.run_id == Run.id)
        .where(Run.started_at >= start)
    )
    if project_id is not None:
        stmt = stmt.join(Ticket, Run.ticket_id == Ticket.id).where(
            Ticket.project_id == project_id
        )
    by_day: dict[str, dict[str, list[float]]] = {}
    for day, model, lats in session.execute(stmt).all():
        floats = _as_floats(lats)
        if not floats:
            continue
        by_day.setdefault(str(day), {}).setdefault(
            model or "unknown", []
        ).extend(floats)

    out: list[dict] = []
    day = start_of_day(start, tz).astimezone(tz)
    last = start_of_day(now, tz).astimezone(tz)
    while day <= last:
        key = day.strftime("%Y-%m-%d")
        per_model = {
            m: statistics.median(lats)
            for m, lats in by_day.get(key, {}).items() if lats
        }
        out.append({"date": key, "by_model": per_model})
        day += timedelta(days=1)
    return out


def project_rollups(
    session: Session, *, start: datetime, end: Optional[datetime] = None,
    prices: Optional[PriceInfo] = None, project_id: Optional[str] = None,
) -> list[dict]:
    """Per-project spend/tokens/runs/success-rate rollup over ``[start, end)``.

    Groups by ``(project, model, has_snapshot)`` so cost can be repriced for
    non-snapshot runs while snapshotted runs keep their stored cost, then
    rolls back up to one row per project, most tokens first. Tickets with no
    project group under a ``None`` id / ``"(no project)"`` name so every run
    is accounted for. ``project_id`` restricts the rollup to a single project
    (the result then has at most one row) — the same filter the other
    aggregates accept. Each row carries ``estimated_runs``/``estimated``
    (see ``tokens_by_model``).
    """
    stmt = (
        select(
            Ticket.project_id,
            Project.name,
            Project.slug,
            Run.model_used,
            _HAS_SNAPSHOT,
            *_TOKEN_SUMS,
            func.count(Run.id),
            func.coalesce(func.sum(Run.cost_usd), 0.0),
            func.sum(case((Run.exit_status == "success", 1), else_=0)),
            func.sum(case((Run.finished_at.is_not(None), 1), else_=0)),
        )
        .select_from(Run)
        .join(Ticket, Run.ticket_id == Ticket.id)
        .outerjoin(Project, Ticket.project_id == Project.id)
        .where(Run.started_at >= start)
        .group_by(Ticket.project_id, Project.name, Project.slug, Run.model_used, _HAS_SNAPSHOT)
    )
    if end is not None:
        stmt = stmt.where(Run.started_at < end)
    if project_id is not None:
        stmt = stmt.where(Ticket.project_id == project_id)
    agg: dict[Optional[str], dict] = {}
    names: dict[Optional[str], tuple[Optional[str], Optional[str]]] = {}
    for pid, name, slug, model, has_snap, it, ot, cr, cw, n, cost, success, completed in session.execute(stmt).all():
        names[pid] = (name, slug)
        slot = agg.setdefault(pid, {
            "it": 0, "ot": 0, "cr": 0, "cw": 0, "n": 0, "cost": 0.0,
            "success": 0, "completed": 0, "estimated_runs": 0,
        })
        it, ot, cr, cw, n = int(it), int(ot), int(cr), int(cw), int(n)
        c, is_est = _split_cost(model, it, ot, cr, cw, bool(has_snap), float(cost), prices)
        slot["it"] += it
        slot["ot"] += ot
        slot["cr"] += cr
        slot["cw"] += cw
        slot["n"] += n
        slot["success"] += int(success or 0)
        slot["completed"] += int(completed or 0)
        slot["cost"] += c
        if is_est:
            slot["estimated_runs"] += n
    rows = []
    for pid, a in agg.items():
        name, slug = names[pid]
        completed = a["completed"]
        rows.append({
            "project_id": pid,
            "project_name": name or "(no project)",
            "project_slug": slug,
            **_token_row(a["it"], a["ot"], a["cr"], a["cw"], a["n"], a["cost"]),
            "success_rate": (a["success"] / completed) if completed else 0.0,
            "estimated_runs": a["estimated_runs"],
            "estimated": a["estimated_runs"] > 0,
        })
    rows.sort(key=lambda r: r["total_tokens"], reverse=True)
    return rows


def build_dashboard(
    session: Session, *, now: datetime,
    price_info: Optional[PriceInfo] = None,
    tz: ZoneInfo = _UTC,
    project_id: Optional[str] = None,
) -> dict:
    """Assemble the full context for the ``/analytics`` page.

    Breakdowns and stats use a rolling 30-day window; the headline chips use
    today / 7-day / 30-day windows. Token-focused: cost is a secondary figure.

    ``tz`` localizes every day/week/month boundary (default UTC = original
    behavior); ``now`` stays an aware UTC instant internally. ``price_info``
    drives both the displayed cost (repriced from the resolved live/cached/table
    prices) and the source label. Defaults to the bundled table when unset
    (e.g. direct unit tests with no network). ``project_id`` scopes every
    aggregate below to one project's tickets; ``by_project`` (the cross-project
    rollup) respects it too, so passing it degenerates to that project's own
    single-row breakdown.
    """
    if price_info is None:
        price_info = pricing.table_price_info()

    today_start = start_of_day(now, tz)
    last_7d_start = today_start - timedelta(days=6)
    last_30d_start = today_start - timedelta(days=29)

    by_model = tokens_by_model(
        session, start=last_30d_start, prices=price_info, project_id=project_id,
    )
    # Assign each model a stable color by token rank; reuse it in the legend,
    # the per-model table, and the stacked daily bars.
    model_colors = {
        row["model"]: MODEL_PALETTE[i % len(MODEL_PALETTE)]
        for i, row in enumerate(by_model)
    }
    for row in by_model:
        row["color"] = model_colors[row["model"]]
    model_legend = [{"model": m, "color": c} for m, c in model_colors.items()]

    series = daily_usage_by_model_series(
        session, start=last_30d_start, now=now, prices=price_info, tz=tz,
        project_id=project_id,
    )
    max_daily_tokens = max((d["total_tokens"] for d in series), default=0)

    # Latency rollups (read cached run_latency rows, never transcript files).
    latency_rows = latency_by_model(session, start=last_30d_start, project_id=project_id)
    latency_series = daily_latency_by_model_series(
        session, start=last_30d_start, now=now, tz=tz, project_id=project_id,
    )
    model_tool = model_vs_tool_time(session, start=last_30d_start, project_id=project_id)
    max_daily_latency = max(
        (med for d in latency_series for med in d["by_model"].values()),
        default=0.0,
    )
    # Reuse the token-rank colors so a given model keeps one color across every
    # chart; latency-only models (no tokens) get the next palette slots.
    latency_colors = dict(model_colors)
    next_idx = len(model_colors)
    for row in latency_rows:
        m = row["model"]
        if m not in latency_colors:
            latency_colors[m] = MODEL_PALETTE[next_idx % len(MODEL_PALETTE)]
            next_idx += 1
    latency_model_legend = [{"model": m, "color": c}
                            for m, c in latency_colors.items()]
    for row in latency_rows:
        row["color"] = latency_colors[row["model"]]

    return {
        "price_source": price_info.source,
        "price_as_of": price_info.as_of,
        "price_source_label": price_info.label,
        # Kept for backward compatibility with older templates/tests.
        "prices_as_of": price_info.as_of,
        "today": window_totals(session, start=today_start, prices=price_info, project_id=project_id),
        "last_7d": window_totals(session, start=last_7d_start, prices=price_info, project_id=project_id),
        "last_30d": window_totals(session, start=last_30d_start, prices=price_info, project_id=project_id),
        "by_model": by_model,
        "model_legend": model_legend,
        "by_profile": usage_by_profile(
            session, start=last_30d_start, prices=price_info, project_id=project_id,
        ),
        "by_ticket": usage_by_ticket(
            session, start=last_30d_start, prices=price_info, project_id=project_id,
        ),
        "by_project": project_rollups(
            session, start=last_30d_start, prices=price_info, project_id=project_id,
        ),
        "run_stats": run_stats(session, start=last_30d_start, project_id=project_id),
        "duration": duration_percentiles(session, start=last_30d_start, project_id=project_id),
        "daily_series": series,
        "max_daily_tokens": max_daily_tokens,
        "latency_by_model": latency_rows,
        "latency_series": latency_series,
        "latency_model_legend": latency_model_legend,
        "max_daily_latency": max_daily_latency,
        "model_vs_tool_time": model_tool,
    }
