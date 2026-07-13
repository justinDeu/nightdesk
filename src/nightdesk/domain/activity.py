"""Unified project activity feed — docs/design/project-control-plane.md §History.

One reverse-chronological, cursor-paginated stream merging four event sources
scoped to a single project:

- **runs**        — agent run outcomes (status, duration, tokens, cost)
- **lifecycle**   — ticket transitions into ``review`` / ``archived``
                    (the outcome states the user reviews; from ``ticket_events``)
- **repo**        — MR/issue state snapshots (opened/merged/closed) from
                    ``external_links`` — the only persistently-synced repo state
- **cron**        — cron fires from ``cron_job_fires`` (scoped by the cron's
                    ``source_path`` matching the project's)

Filters (``kind``) and text search (``q``) are applied **server-side** per
source before the merge, never client-side over a loaded window — that is what
keeps the Failures chip honest about anything past the first page.

Pagination is a single opaque cursor carrying ``(ts, id)`` of the oldest row on
the current page. "Load earlier" fetches, per source, rows strictly older in the
global ``(ts DESC, id DESC)`` order, then re-merges. Each source pulls
``limit + 1`` rows so a leftover anywhere surfaces as ``has_more`` on the merged
page — the standard fan-in pagination trick.

Week-boundary rollups (``include_rollups``) are a companion aggregate, computed
once on the first page (cursor absent) over the last ``ROLLUP_WINDOW`` days for
the active filter. They are numbers-only (runs / shipped / cost / success%) and
intentionally carry no generated prose (design ruling: no prose digests).
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from nightdesk.db.models import CronJob, CronJobFire, ExternalLink, Run, Ticket, TicketEvent
from nightdesk.domain.projects import ProjectNotFound, get_project


# ---------------------------------------------------------------------------
# Kinds + filter matrix
# ---------------------------------------------------------------------------

# The underlying event kinds surfacing in the feed. The pill + glyph the UI
# renders are derived from these; the filter chips below map onto them.
KIND_RUN = "run"
KIND_LIFECYCLE = "lifecycle"
KIND_REPO = "repo"
KIND_CRON = "cron"

# Lifecycle transitions worth surfacing as History rows. The complete
# ticket_events audit log also records queued->running etc.; those are runtime
# churn, not outcomes a user reviews, so they are excluded here.
LIFECYCLE_STATUSES = ("review", "archived")
SHIPPED_STATUSES = ("archived",)

# External-link states that count as "shipped" work.
MR_MERGED_STATE = "merged"

# Filter chip -> which underlying kinds/predicates the feed includes.
VALID_FILTERS = ("all", "runs", "failures", "shipped", "repo", "lifecycle")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Rollups cover this trailing window (≈ six months). Bounded so the aggregate
# stays cheap even for long-lived projects; the feed itself paginates further
# back via "Load earlier".
ROLLUP_WINDOW = timedelta(days=26 * 7)


@dataclass
class ActivityItem:
    """One merged feed row. Fields are optional beyond kind/ts/title/id; each
    source populates only its own. ``order_key`` is the stable global sort key
    ``(ts DESC, id DESC)`` used for merging + cursor pagination."""

    id: str
    kind: str
    ts: datetime
    title: str

    # run-specific
    outcome: Optional[str] = None  # success | failed | running | unknown
    duration_seconds: Optional[float] = None
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    # links back into the app
    run_id: Optional[str] = None
    ticket_id: Optional[str] = None
    # lifecycle-specific
    to_status: Optional[str] = None
    # repo-specific
    repo_kind: Optional[str] = None  # merge_request | issue
    repo_link_id: Optional[str] = None
    external_iid: Optional[str] = None
    external_url: Optional[str] = None
    state: Optional[str] = None  # opened | merged | closed
    diff_add: Optional[int] = None
    diff_del: Optional[int] = None
    # cron-specific
    skipped_reason: Optional[str] = None

    def order_key(self) -> tuple[datetime, str]:
        # Global reverse-chronological order: newer ts first, then larger id.
        return (self.ts, self.id)


@dataclass
class WeekRollup:
    """Numbers-only week-boundary rollup (Mon–Sun, UTC)."""

    week_start: datetime  # midnight UTC Monday
    runs: int = 0
    failures: int = 0
    shipped: int = 0
    cost_usd: float = 0.0
    success_rate: float = 0.0  # 0..1 of *completed* runs


@dataclass
class ActivityFeed:
    items: list[ActivityItem] = field(default_factory=list)
    rollups: list[WeekRollup] = field(default_factory=list)
    next_cursor: Optional[str] = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite drops tzinfo; treat naive stored datetimes as UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _escape_like(q: str) -> str:
    # Escape LIKE wildcards so user input is literal. Backslash is the escape char.
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _monday(dt: datetime) -> datetime:
    """Floor an aware datetime to its UTC Monday 00:00 (ISO-week start)."""
    dt = _aware(dt) or dt
    return (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

_SEP = "\x1f"


def encode_cursor(ts: datetime, item_id: str) -> str:
    raw = f"{ts.astimezone(timezone.utc).isoformat()}{_SEP}{item_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> Optional[tuple[datetime, str]]:
    """Return ``(ts, id)`` or ``None`` if the cursor is malformed/expired."""
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + pad).encode()).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if _SEP not in raw:
        return None
    ts_part, _, item_id = raw.partition(_SEP)
    try:
        ts = datetime.fromisoformat(ts_part)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if not item_id:
        return None
    return ts, item_id


def _before_clause(ts_expr, id_col, cursor: tuple[datetime, str]):
    """Rows strictly older than the cursor in global (ts DESC, id DESC) order."""
    cts, cid = cursor
    return or_(
        ts_expr < cts,
        and_(ts_expr == cts, id_col < cid),
    )


# ---------------------------------------------------------------------------
# Source fetchers
# ---------------------------------------------------------------------------

def _ticket_title_ilike(q: str):
    return Ticket.title.ilike(f"%{_escape_like(q)}%", escape="\\")


def _fetch_runs(
    session: Session, project_id: str, *, kind: str, q: Optional[str],
    cursor: Optional[tuple[datetime, str]], limit: int,
) -> list[ActivityItem]:
    if kind not in ("all", "runs", "failures"):
        return []
    ts_expr = func.coalesce(Run.finished_at, Run.started_at)
    stmt = (
        select(Run)
        .join(Ticket, Run.ticket_id == Ticket.id)
        .where(Ticket.project_id == project_id)
    )
    # An in-flight run (no finished_at, NULL exit_status) is an outcome the
    # user wants at the top; keep it for "runs"/"all" but it is never a failure.
    if kind == "failures":
        stmt = stmt.where(
            Run.finished_at.is_not(None),
            Run.exit_status.is_not(None),
            Run.exit_status != "success",
        )
    if q:
        stmt = stmt.where(_ticket_title_ilike(q))
    if cursor is not None:
        stmt = stmt.where(_before_clause(ts_expr, Run.id, cursor))
    stmt = stmt.order_by(ts_expr.desc(), Run.id.desc()).limit(limit)
    # Read Ticket.title in the parent query — the .join(Ticket) above only
    # filters; it does NOT hydrate the lazy ``run.ticket`` relationship, so
    # touching it per row would N+1 (one ticket SELECT per run). ticket_id is
    # on the run itself; the title is the only field we need from the ticket.
    out: list[ActivityItem] = []
    for run, title in session.execute(stmt.add_columns(Ticket.title)):
        out.append(_run_to_item(run, title))
    return out


def _run_to_item(run: Run, title: str) -> ActivityItem:
    aware_started = _aware(run.started_at)
    aware_finished = _aware(run.finished_at)
    duration = None
    if aware_started and aware_finished:
        duration = (aware_finished - aware_started).total_seconds()
    tokens = (run.input_tokens or 0) + (run.output_tokens or 0)
    if run.exit_status == "success":
        outcome = "success"
    elif aware_finished is None:
        outcome = "running"
    elif run.exit_status:
        outcome = "failed"
    else:
        outcome = "unknown"
    ts = aware_finished or aware_started
    return ActivityItem(
        id=run.id,
        kind=KIND_RUN,
        ts=ts or datetime.now(timezone.utc),
        title=title or "",
        outcome=outcome,
        duration_seconds=duration,
        tokens=tokens or None,
        cost_usd=run.cost_usd,
        run_id=run.id,
        ticket_id=run.ticket_id,
    )


def _fetch_lifecycle(
    session: Session, project_id: str, *, kind: str, q: Optional[str],
    cursor: Optional[tuple[datetime, str]], limit: int,
) -> list[ActivityItem]:
    # "shipped" includes archived lifecycle events; "lifecycle" shows both
    # review + archived; the other chips exclude this source.
    if kind == "shipped":
        statuses = SHIPPED_STATUSES
    elif kind == "lifecycle":
        statuses = LIFECYCLE_STATUSES
    elif kind == "all":
        statuses = LIFECYCLE_STATUSES
    else:
        return []
    stmt = (
        select(TicketEvent)
        .join(Ticket, TicketEvent.ticket_id == Ticket.id)
        .where(Ticket.project_id == project_id)
        .where(TicketEvent.to_status.in_(statuses))
    )
    if q:
        stmt = stmt.where(_ticket_title_ilike(q))
    if cursor is not None:
        stmt = stmt.where(_before_clause(TicketEvent.created_at, TicketEvent.id, cursor))
    stmt = stmt.order_by(TicketEvent.created_at.desc(), TicketEvent.id.desc()).limit(limit)
    out: list[ActivityItem] = []
    # TicketEvent is a flat audit row with no ORM relationship to its ticket;
    # join Ticket to read the title (already joined for the project filter).
    for ev, title in session.execute(stmt.add_columns(Ticket.title)):
        out.append(ActivityItem(
            id=ev.id,
            kind=KIND_LIFECYCLE,
            ts=_aware(ev.created_at) or datetime.now(timezone.utc),
            title=title or "",
            to_status=ev.to_status,
            ticket_id=ev.ticket_id,
            run_id=ev.run_id,
        ))
    return out


def _fetch_repo(
    session: Session, project_id: str, *, kind: str, q: Optional[str],
    cursor: Optional[tuple[datetime, str]], limit: int,
) -> list[ActivityItem]:
    if kind not in ("all", "shipped", "repo"):
        return []
    stmt = (
        select(ExternalLink)
        .join(Ticket, ExternalLink.ticket_id == Ticket.id)
        .where(Ticket.project_id == project_id)
        .where(ExternalLink.kind.in_(("merge_request", "issue")))
        .where(ExternalLink.state.is_not(None))
    )
    if kind == "shipped":
        # Only MRs that merged count as "shipped" on the repo side.
        stmt = stmt.where(ExternalLink.kind == "merge_request", ExternalLink.state == MR_MERGED_STATE)
    if q:
        like = f"%{_escape_like(q)}%"
        stmt = stmt.where(
            or_(
                ExternalLink.title.ilike(like, escape="\\"),
                ExternalLink.external_iid.ilike(like, escape="\\"),
            )
        )
    if cursor is not None:
        stmt = stmt.where(_before_clause(ExternalLink.updated_at, ExternalLink.id, cursor))
    stmt = stmt.order_by(ExternalLink.updated_at.desc(), ExternalLink.id.desc()).limit(limit)
    out: list[ActivityItem] = []
    for link in session.scalars(stmt):
        add, dele = _diff_stats(link)
        out.append(ActivityItem(
            id=link.id,
            kind=KIND_REPO,
            ts=_aware(link.updated_at) or datetime.now(timezone.utc),
            title=link.title or "",
            repo_kind=link.kind,
            repo_link_id=link.repo_link_id,
            external_iid=link.external_iid,
            external_url=link.url or None,
            state=link.state,
            diff_add=add,
            diff_del=dele,
            ticket_id=link.ticket_id,
        ))
    return out


def _diff_stats(link: ExternalLink) -> tuple[Optional[int], Optional[int]]:
    """Best-effort +/− diffstat from a link's cached ``state_detail`` blob.

    GitLab's MR detail lands provider-specific keys here; we tolerate any of the
    common shapes and return ``(None, None)`` when nothing is recognizable so the
    UI simply omits the stat rather than guessing.
    """
    detail = link.state_detail
    if not isinstance(detail, dict):
        return None, None
    add = detail.get("additions", detail.get("lines_added"))
    dele = detail.get("deletions", detail.get("lines_removed"))
    changes = detail.get("changes_count")
    if add is None and isinstance(changes, str) and changes.strip().isdigit():
        # Some adapters stash a single changes count; treat as additions only.
        add = int(changes.strip())
    return (
        int(add) if isinstance(add, (int, float)) and add >= 0 else None,
        int(dele) if isinstance(dele, (int, float)) and dele >= 0 else None,
    )


def _fetch_cron(
    session: Session, project_id: str, *, kind: str, q: Optional[str],
    cursor: Optional[tuple[datetime, str]], limit: int,
) -> list[ActivityItem]:
    if kind not in ("all", "runs"):
        # Cron fires are "all"-feed context; they don't belong under the
        # outcome-oriented chips (runs/failures/shipped/repo/lifecycle).
        return []
    # Crons are not project-keyed; scope by the cron's source_path matching the
    # project's. ``get_project`` was already called by the entry point so this
    # hits the identity-map cache.
    proj = get_project(session, project_id)
    stmt = (
        select(CronJobFire)
        .join(CronJob, CronJobFire.cron_job_id == CronJob.id)
        .where(CronJob.source_path == proj.source_path)
    )
    if q:
        like = f"%{_escape_like(q)}%"
        stmt = stmt.where(CronJob.title.ilike(like, escape="\\"))
    if cursor is not None:
        stmt = stmt.where(_before_clause(CronJobFire.fire_at, CronJobFire.id, cursor))
    stmt = stmt.order_by(CronJobFire.fire_at.desc(), CronJobFire.id.desc()).limit(limit)
    # Read CronJob.title in the parent query — the .join(CronJob) above only
    # filters (for source_path / q); it does NOT hydrate ``fire.cron_job``, so
    # touching it per row would N+1 (one cron_jobs SELECT per fire).
    out: list[ActivityItem] = []
    for fire, title in session.execute(stmt.add_columns(CronJob.title)):
        out.append(ActivityItem(
            id=fire.id,
            kind=KIND_CRON,
            ts=_aware(fire.fire_at) or datetime.now(timezone.utc),
            title=title or "cron",
            ticket_id=fire.ticket_id,
            skipped_reason=fire.skipped_reason,
        ))
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def project_activity_feed(
    session: Session,
    project_id: str,
    *,
    kind: str = "all",
    q: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    include_rollups: bool = False,
) -> ActivityFeed:
    """Build the merged, cursor-paginated activity feed for one project.

    Raises :class:`ProjectNotFound` (the route maps it to 404).
    """
    # Validate the project up front (also primes the identity map for the cron
    # source_path lookup).
    get_project(session, project_id)

    if kind not in VALID_FILTERS:
        kind = "all"
    limit = max(1, min(limit, MAX_LIMIT))
    q = q.strip() if q else None
    decoded = decode_cursor(cursor) if cursor else None

    fetch_limit = limit + 1  # +1 detects a leftover in any source
    sources = [
        _fetch_runs(session, project_id, kind=kind, q=q, cursor=decoded, limit=fetch_limit),
        _fetch_lifecycle(session, project_id, kind=kind, q=q, cursor=decoded, limit=fetch_limit),
        _fetch_repo(session, project_id, kind=kind, q=q, cursor=decoded, limit=fetch_limit),
        _fetch_cron(session, project_id, kind=kind, q=q, cursor=decoded, limit=fetch_limit),
    ]
    merged: list[ActivityItem] = []
    for batch in sources:
        merged.extend(batch)
    # Global order: ts DESC, id DESC. id is a globally-unique UUID string, so the
    # tiebreak is a stable total order across heterogeneous sources.
    merged.sort(key=lambda it: it.order_key(), reverse=True)

    has_more = len(merged) > limit
    page = merged[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(last.ts, last.id)

    rollups: list[WeekRollup] = []
    if include_rollups and decoded is None:
        rollups = _compute_rollups(session, project_id, kind=kind, q=q)

    return ActivityFeed(items=page, rollups=rollups, next_cursor=next_cursor, has_more=has_more)


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------

def _compute_rollups(
    session: Session, project_id: str, *, kind: str, q: Optional[str],
) -> list[WeekRollup]:
    """Numbers-only weekly aggregates over the trailing ROLLUP_WINDOW, for the
    active filter. Computed from lightweight per-source rows (ts + a couple of
    discriminators) bucketed to ISO-week Mondays in Python."""
    since = datetime.now(timezone.utc) - ROLLUP_WINDOW
    buckets: dict[datetime, WeekRollup] = {}
    # Successes per week, tracked separately from WeekRollup (success_rate is
    # derived). Kept apart so the rate is over COMPLETED runs only — in-flight
    # runs (NULL exit_status) count toward `runs` but must not inflate success%.
    successes: dict[datetime, int] = {}

    def bucket(monday: datetime) -> WeekRollup:
        return buckets.setdefault(monday, WeekRollup(week_start=monday))

    # Runs contribute run/failure/cost/success counts.
    if kind in ("all", "runs", "failures"):
        ts_expr = func.coalesce(Run.finished_at, Run.started_at)
        stmt = (
            select(Run.exit_status, Run.cost_usd, ts_expr)
            .join(Ticket, Run.ticket_id == Ticket.id)
            .where(Ticket.project_id == project_id)
            .where(ts_expr >= since)
            .order_by(ts_expr.desc())
        )
        if kind == "failures":
            stmt = stmt.where(
                Run.finished_at.is_not(None),
                Run.exit_status.is_not(None),
                Run.exit_status != "success",
            )
        if q:
            stmt = stmt.where(_ticket_title_ilike(q))
        for exit_status, cost, ts in session.execute(stmt):
            if ts is None:
                continue
            monday = _monday(ts)
            b = bucket(monday)
            b.runs += 1
            if exit_status == "success":
                successes[monday] = successes.get(monday, 0) + 1
            elif exit_status is not None:
                b.failures += 1
            if cost:
                b.cost_usd += float(cost)

    # Shipped = archived lifecycle events (repo-merged MRs also count, below).
    if kind in ("all", "shipped"):
        ev_stmt = (
            select(TicketEvent.created_at)
            .join(Ticket, TicketEvent.ticket_id == Ticket.id)
            .where(Ticket.project_id == project_id)
            .where(TicketEvent.to_status.in_(SHIPPED_STATUSES))
            .where(TicketEvent.created_at >= since)
        )
        if q:
            ev_stmt = ev_stmt.where(_ticket_title_ilike(q))
        for (ts,) in session.execute(ev_stmt):
            if ts is None:
                continue
            bucket(_monday(ts)).shipped += 1

    # Repo-merged MRs count as shipped for the "all"/"shipped" views.
    if kind in ("all", "shipped"):
        mr_stmt = (
            select(ExternalLink.updated_at)
            .join(Ticket, ExternalLink.ticket_id == Ticket.id)
            .where(Ticket.project_id == project_id)
            .where(ExternalLink.kind == "merge_request")
            .where(ExternalLink.state == MR_MERGED_STATE)
            .where(ExternalLink.updated_at >= since)
        )
        if q:
            like = f"%{_escape_like(q)}%"
            mr_stmt = mr_stmt.where(
                or_(
                    ExternalLink.title.ilike(like, escape="\\"),
                    ExternalLink.external_iid.ilike(like, escape="\\"),
                )
            )
        for (ts,) in session.execute(mr_stmt):
            if ts is None:
                continue
            bucket(_monday(ts)).shipped += 1

    # Finalize success rates: successes / (successes + failures), over COMPLETED
    # runs only. In-flight runs count toward `runs` but are excluded from the
    # denominator so a busy week of still-running agents doesn't read as 100%.
    for b in buckets.values():
        succ = successes.get(b.week_start, 0)
        completed = succ + b.failures
        b.success_rate = (succ / completed) if completed > 0 else 0.0
    return sorted(buckets.values(), key=lambda b: b.week_start, reverse=True)
