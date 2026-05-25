"""Recurring cron jobs: schedule-to-ticket automation.

A cron job is a ticket template plus a schedule. When the schedule fires the
worker materializes one ordinary ``status="queued"``, ``run_now=False`` ticket
from the template (see :func:`materialize_due_cron_jobs`). The generated ticket
then flows through the normal scheduler, inheriting the active-hours window and
``max_parallel`` capacity. Cron does NOT bypass the scheduler.

Backend neutrality: the agent backend is selected by ``Profile.backend``, never
by the cron job. The template copies only ``profile_id`` and stores no engine
field, so a profile pointed at a non-Claude backend produces tickets that run on
that backend with zero cron changes.

Schedule semantics:
- Standard 5-field cron (``minute hour dom month dow``); 6-field and other arities
  are rejected even though ``croniter.is_valid`` would accept them.
- Timezone is an IANA name (``zoneinfo``). Next-fire is computed in the job
  timezone; all DB datetimes are stored in UTC.
- Default ``misfire_policy="coalesce"``: one ticket for the oldest due fire, then
  advance past now. No backfill in v1.
- Default ``overlap_policy="skip_if_active"``: skip a new ticket if the previous
  generated ticket is draft/queued/running. ``review``/``archived`` do not block.

Workspace is directory-only in v1: ``workspace_mode`` is constrained to
``directory``/``in_place``; ``git_worktree``/``worktree`` is rejected.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nightdesk.db.models import CronJob, CronJobFire, Ticket
from nightdesk.domain.tickets import create_ticket


class CronJobNotFound(Exception):
    pass


class InvalidCronJob(ValueError):
    """Raised for bad schedule / timezone / workspace. Map to HTTP 422."""


_ALLOWED_WORKSPACE_MODES = ("directory", "in_place")
_MISFIRE_POLICIES = ("coalesce",)
_OVERLAP_POLICIES = ("skip_if_active", "always")
# A previous generated ticket in any of these states blocks a new fire under
# skip_if_active. 'review' and 'archived' (and a deleted/missing ticket) do not.
_OVERLAP_BLOCKING_STATUSES = {"draft", "queued", "running"}


# --- validation --------------------------------------------------------------


def validate_schedule(schedule: str) -> str:
    """Validate a standard 5-field cron expression.

    ``croniter.is_valid`` accepts 6-field (seconds) and other extensions, so we
    explicitly require exactly 5 whitespace-separated fields first.
    """
    if not isinstance(schedule, str) or not schedule.strip():
        raise InvalidCronJob("schedule is required")
    expr = schedule.strip()
    if len(expr.split()) != 5:
        raise InvalidCronJob(
            "schedule must be a standard 5-field cron expression "
            "(minute hour day-of-month month day-of-week)"
        )
    if not croniter.is_valid(expr):
        raise InvalidCronJob(f"invalid cron expression: {expr!r}")
    return expr


def validate_timezone(tz: str) -> str:
    if not isinstance(tz, str) or not tz.strip():
        raise InvalidCronJob("timezone is required")
    name = tz.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as e:
        raise InvalidCronJob(f"unknown timezone: {name!r}") from e
    return name


def validate_workspace_mode(mode: str) -> str:
    if mode in ("git_worktree", "worktree"):
        raise InvalidCronJob(
            "cron jobs are directory-only in v1; worktree workspaces are not "
            "supported (create a one-off ticket for worktree isolation)"
        )
    if mode not in _ALLOWED_WORKSPACE_MODES:
        raise InvalidCronJob(
            f"workspace_mode must be one of {_ALLOWED_WORKSPACE_MODES}, got {mode!r}"
        )
    return mode


def _validate_additional_dirs(dirs: Optional[list]) -> list:
    """Reject any git_worktree workspace entry smuggled in via additional_dirs."""
    out = list(dirs or [])
    for d in out:
        kind = d.get("kind") if isinstance(d, dict) else None
        if kind in ("git_worktree", "worktree"):
            raise InvalidCronJob("git_worktree workspaces are not supported for cron jobs")
    return out


# --- next-fire computation ----------------------------------------------------


def compute_next_fire(schedule: str, tz: str, after: datetime) -> datetime:
    """Return the next fire strictly after ``after``, as an aware UTC datetime.

    Computed in the job timezone, then converted to UTC for storage.
    """
    zone = ZoneInfo(tz)
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    base_local = after.astimezone(zone)
    it = croniter(schedule, base_local)
    nxt_local = it.get_next(datetime)
    return nxt_local.astimezone(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --- CRUD ---------------------------------------------------------------------


def create_cron_job(
    session: Session,
    *,
    title: str,
    prompt: str = "",
    profile_id: str,
    cwd: str,
    schedule: str,
    timezone: str = "UTC",
    priority: int = 0,
    workspace_mode: str = "directory",
    additional_dirs: Optional[list] = None,
    permission_overrides: Optional[dict] = None,
    enabled: bool = True,
    misfire_policy: str = "coalesce",
    overlap_policy: str = "skip_if_active",
    now: Optional[datetime] = None,
) -> CronJob:
    if not isinstance(title, str) or not title.strip():
        raise InvalidCronJob("title is required")
    if not isinstance(cwd, str) or not cwd.strip():
        raise InvalidCronJob("cwd is required")
    schedule = validate_schedule(schedule)
    tz = validate_timezone(timezone)
    workspace_mode = validate_workspace_mode(workspace_mode)
    additional_dirs = _validate_additional_dirs(additional_dirs)
    if misfire_policy not in _MISFIRE_POLICIES:
        raise InvalidCronJob(f"misfire_policy must be one of {_MISFIRE_POLICIES}")
    if overlap_policy not in _OVERLAP_POLICIES:
        raise InvalidCronJob(f"overlap_policy must be one of {_OVERLAP_POLICIES}")

    now = _as_utc(now) or _utcnow()
    job = CronJob(
        title=title,
        prompt=prompt or "",
        profile_id=profile_id,
        cwd=cwd,
        priority=priority,
        workspace_mode=workspace_mode,
        additional_dirs=additional_dirs,
        permission_overrides=permission_overrides,
        schedule=schedule,
        timezone=tz,
        enabled=enabled,
        misfire_policy=misfire_policy,
        overlap_policy=overlap_policy,
        next_fire_at=compute_next_fire(schedule, tz, now) if enabled else None,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def get_cron_job(session: Session, cron_job_id: str) -> CronJob:
    job = session.get(CronJob, cron_job_id)
    if job is None:
        raise CronJobNotFound(cron_job_id)
    return job


def list_cron_jobs(session: Session, *, enabled: Optional[bool] = None) -> list[CronJob]:
    stmt = select(CronJob).order_by(CronJob.created_at.asc())
    if enabled is not None:
        stmt = stmt.where(CronJob.enabled.is_(enabled))
    return list(session.scalars(stmt))


def update_cron_job(
    session: Session, cron_job_id: str, *, now: Optional[datetime] = None, **fields
) -> CronJob:
    job = get_cron_job(session, cron_job_id)
    schedule_changed = False

    if "title" in fields:
        if not isinstance(fields["title"], str) or not fields["title"].strip():
            raise InvalidCronJob("title cannot be empty")
    if "cwd" in fields:
        if not isinstance(fields["cwd"], str) or not fields["cwd"].strip():
            raise InvalidCronJob("cwd cannot be empty")
    if "schedule" in fields:
        fields["schedule"] = validate_schedule(fields["schedule"])
        schedule_changed = fields["schedule"] != job.schedule
    if "timezone" in fields:
        fields["timezone"] = validate_timezone(fields["timezone"])
        schedule_changed = schedule_changed or fields["timezone"] != job.timezone
    if "workspace_mode" in fields:
        fields["workspace_mode"] = validate_workspace_mode(fields["workspace_mode"])
    if "additional_dirs" in fields:
        fields["additional_dirs"] = _validate_additional_dirs(fields["additional_dirs"])
    if "misfire_policy" in fields and fields["misfire_policy"] not in _MISFIRE_POLICIES:
        raise InvalidCronJob(f"misfire_policy must be one of {_MISFIRE_POLICIES}")
    if "overlap_policy" in fields and fields["overlap_policy"] not in _OVERLAP_POLICIES:
        raise InvalidCronJob(f"overlap_policy must be one of {_OVERLAP_POLICIES}")

    for k, v in fields.items():
        setattr(job, k, v)

    # Recompute next_fire_at when the schedule or timezone changes (only while
    # enabled). disable/enable have their own helpers.
    if schedule_changed and job.enabled:
        now = _as_utc(now) or datetime.now(timezone.utc)
        job.next_fire_at = compute_next_fire(job.schedule, job.timezone, now)

    session.commit()
    session.refresh(job)
    return job


def enable_cron_job(session: Session, cron_job_id: str, *, now: Optional[datetime] = None) -> CronJob:
    """Enable a job and recompute ``next_fire_at`` from now."""
    job = get_cron_job(session, cron_job_id)
    job.enabled = True
    now = _as_utc(now) or datetime.now(timezone.utc)
    job.next_fire_at = compute_next_fire(job.schedule, job.timezone, now)
    session.commit()
    session.refresh(job)
    return job


def disable_cron_job(session: Session, cron_job_id: str) -> CronJob:
    """Disable a job. ``next_fire_at`` is left UNCHANGED — the stale value is
    harmless because :func:`materialize_due_cron_jobs` filters ``enabled``."""
    job = get_cron_job(session, cron_job_id)
    job.enabled = False
    session.commit()
    session.refresh(job)
    return job


def delete_cron_job(session: Session, cron_job_id: str) -> None:
    """Remove the schedule (and its fire/audit rows). Generated tickets are NOT
    deleted — they are ordinary tickets that outlive the schedule."""
    job = get_cron_job(session, cron_job_id)
    # Detach generated tickets' back-reference is unnecessary (tickets don't
    # point at the cron job). Cascade drops the cron_job_fires rows.
    session.delete(job)
    session.commit()


# --- materialization ----------------------------------------------------------


def _ticket_from_template(session: Session, job: CronJob) -> Ticket:
    """Create an ordinary queued, run_now=false ticket from the template.

    NEVER sets ``run_now=True`` — the generated ticket must wait for the normal
    scheduler (window + capacity).
    """
    return create_ticket(
        session,
        title=job.title,
        prompt=job.prompt,
        status="queued",
        run_now=False,
        priority=job.priority,
        profile_id=job.profile_id,
        cwd=job.cwd,
        workspace_mode=job.workspace_mode,
        additional_dirs=list(job.additional_dirs or []),
        permission_overrides=(dict(job.permission_overrides)
                              if job.permission_overrides else None),
    )


def _overlap_blocks(session: Session, job: CronJob) -> bool:
    """True if ``skip_if_active`` should suppress a new fire because the last
    generated ticket is still draft/queued/running."""
    if job.overlap_policy != "skip_if_active":
        return False
    if not job.last_ticket_id:
        return False
    last = session.get(Ticket, job.last_ticket_id)
    if last is None:
        return False
    return last.status in _OVERLAP_BLOCKING_STATUSES


def _claim_fire(session: Session, job_id: str, fire_at: datetime) -> Optional[CronJobFire]:
    """Insert a fire row to claim ``(job_id, fire_at)``. Returns the row, or
    None if the unique constraint already holds (another tick claimed it).

    The claim is flushed (not committed) so the caller can fill in ticket_id /
    skipped_reason and commit once.
    """
    fire = CronJobFire(cron_job_id=job_id, fire_at=fire_at)
    session.add(fire)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return fire


def fire_now(session: Session, cron_job_id: str, *, now: Optional[datetime] = None) -> Ticket:
    """Manually materialize a ticket from the template immediately.

    Bypasses ``overlap_policy`` (a manual fire is an explicit user request) and
    does NOT set ``run_now=True`` — the ticket still flows through the scheduler.
    Uses sub-minute ``now()`` for ``fire_at`` so it won't collide with a
    minute-granular scheduled fire in the same minute.
    """
    job = get_cron_job(session, cron_job_id)
    fire_at = _as_utc(now) or datetime.now(timezone.utc)
    fire = _claim_fire(session, job.id, fire_at)
    if fire is None:
        # Extraordinarily unlikely (sub-minute collision). Nudge by a microsecond.
        fire_at = fire_at.replace(microsecond=(fire_at.microsecond + 1) % 1000000)
        fire = _claim_fire(session, job.id, fire_at)
        if fire is None:
            raise InvalidCronJob("could not claim a manual fire slot")
    ticket = _ticket_from_template(session, job)
    fire.ticket_id = ticket.id
    job.last_fire_at = fire_at
    job.last_ticket_id = ticket.id
    session.commit()
    session.refresh(ticket)
    return ticket


def materialize_due_cron_jobs(session: Session, now: datetime) -> list[CronJobFire]:
    """Materialize tickets for every enabled cron job whose ``next_fire_at`` is
    due (``<= now``). Returns the fire rows created this call.

    Called by the worker immediately after the heartbeat and BEFORE any backend
    readiness gating: materialization only inserts queued ticket rows and brings
    up no backend, so due jobs become visible queued tickets even while a backend
    is down (they then wait until the worker can run them).

    Misfire (coalesce): one ticket for the oldest due fire, then ``next_fire_at``
    is advanced to the next fire strictly after ``now`` (no backfill).

    Overlap (skip_if_active): if the previous generated ticket is still
    draft/queued/running, record a skipped fire row (ticket_id NULL,
    skipped_reason="overlap") instead of a new ticket — and still advance.

    Idempotent per ``(cron_job_id, fire_at)`` via the unique constraint.
    """
    now = _as_utc(now)
    created: list[CronJobFire] = []

    jobs = list(
        session.scalars(
            select(CronJob).where(
                CronJob.enabled.is_(True),
                CronJob.next_fire_at.is_not(None),
                CronJob.next_fire_at <= now,
            )
        )
    )

    for job in jobs:
        fire_at = _as_utc(job.next_fire_at)
        # Advance immediately so a skip or an exception still moves the schedule
        # forward (coalesce: next fire strictly after now, no backfill).
        next_after = compute_next_fire(job.schedule, job.timezone, now)

        fire = _claim_fire(session, job.id, fire_at)
        if fire is None:
            # Already materialized by a prior tick; just make sure we advance.
            job.next_fire_at = next_after
            session.commit()
            continue

        if _overlap_blocks(session, job):
            fire.skipped_reason = "overlap"
            job.next_fire_at = next_after
            session.commit()
            created.append(fire)
            continue

        ticket = _ticket_from_template(session, job)
        fire.ticket_id = ticket.id
        job.last_fire_at = fire_at
        job.last_ticket_id = ticket.id
        job.next_fire_at = next_after
        session.commit()
        created.append(fire)

    return created


def list_fires(session: Session, cron_job_id: str, *, limit: int = 50) -> list[CronJobFire]:
    return list(
        session.scalars(
            select(CronJobFire)
            .where(CronJobFire.cron_job_id == cron_job_id)
            .order_by(CronJobFire.fire_at.desc())
            .limit(limit)
        )
    )
