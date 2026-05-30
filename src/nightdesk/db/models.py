from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy import (
    String, Integer, Boolean, DateTime, ForeignKey, JSON, Text, Time,
    UniqueConstraint,
)
from sqlalchemy import Float as sa_Float
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, unique=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    fs_read: Mapped[list] = mapped_column(JSON, default=list)
    fs_write: Mapped[list] = mapped_column(JSON, default=list)
    allowed_tools: Mapped[list] = mapped_column(JSON, default=list)
    denied_tools: Mapped[list] = mapped_column(JSON, default=list)
    # v1 narrows network_mode to "off" | "on". Legacy values are migrated by 0010.
    network_mode: Mapped[str] = mapped_column(String, default="off")
    network_allowlist: Mapped[list] = mapped_column(JSON, default=list)
    secret_keys: Mapped[list] = mapped_column(JSON, default=list)
    default_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    backend: Mapped[str] = mapped_column(String, default="claude_sdk", nullable=False)
    # Encrypted JSON blob: {"source": "inherit"|"api_key"|"auth_token", "value": "..."}.
    claude_credentials: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    claude_binary_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Encrypted JSON kv map of custom env vars to inject into the sandbox.
    env: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    permission_mode: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # CC settings.json subset (theme, cleanupPeriodDays, additionalDirectories, etc.).
    cc_settings_passthrough: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # Run-token scopes this profile grants beyond the self-scope defaults.
    run_token_scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    cwd: Mapped[str] = mapped_column(String, nullable=False)
    default_workspace_mode: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_worktree_name_template: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_base_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_linked_workspaces: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    color: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="project")




class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String)
    prompt: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="draft", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"))
    permission_overrides: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    additional_dirs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    cwd: Mapped[str] = mapped_column(String, nullable=False)
    # 'in_place'  -> agent runs in ticket.cwd directly (changes land in your tree)
    # 'worktree'  -> git worktree of cwd under worktree_root (reserved; not yet implemented)
    workspace_mode: Mapped[str] = mapped_column(String, default="in_place", nullable=False)
    run_now: Mapped[bool] = mapped_column(Boolean, default=False)
    scheduled_after: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    current_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    next_run_context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_run_context_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Optional["Project"]] = relationship(back_populates="tickets")
    profile: Mapped["Profile"] = relationship(lazy="joined")
    runs: Mapped[list["Run"]] = relationship(back_populates="ticket", foreign_keys="Run.ticket_id")
    workspaces: Mapped[list["TicketWorkspace"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketWorkspace.position",
    )
    # Dependencies: tickets this ticket must wait for.
    dependencies: Mapped[list["TicketDependency"]] = relationship(
        foreign_keys="TicketDependency.ticket_id",
        back_populates="ticket",
        cascade="all, delete-orphan",
    )
    # Dependents: tickets that wait for this one.
    dependents: Mapped[list["TicketDependency"]] = relationship(
        foreign_keys="TicketDependency.depends_on_id",
        back_populates="depends_on",
        cascade="all, delete-orphan",
    )


class TicketDependency(Base):
    """A directed edge: the ticket owning this row depends on (must wait for)
    ``depends_on_id``.  A dependency is satisfied when the upstream ticket's
    most-recent run has ``exit_status='success'`` AND the upstream is in
    ``review`` or ``archived``."""

    __tablename__ = "ticket_dependencies"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    depends_on_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    ticket: Mapped["Ticket"] = relationship(
        foreign_keys=[ticket_id],
        back_populates="dependencies",
    )
    depends_on: Mapped["Ticket"] = relationship(
        foreign_keys=[depends_on_id],
        back_populates="dependents",
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    error_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    worktree_path: Mapped[str] = mapped_column(String)
    transcript_path: Mapped[str] = mapped_column(String)
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    host: Mapped[str] = mapped_column(String)
    started_as_run_now: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    intent: Mapped[str] = mapped_column(String, default="first_run", nullable=False)
    parent_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    headless_policy_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    restart_workspace_policy: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    failure_kind: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Optional[float]] = mapped_column(sa_Float, nullable=True)
    model_used: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # The base prompt (ticket.prompt at launch time), stored per-run so the
    # transcript panel can show what each run actually used. NULL for runs
    # created before this column existed; display falls back to ticket.prompt.
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Claude session id reported by the SDK at run completion. Enables resuming
    # the conversation (`claude --resume <session_id>` / SDK resume=).
    session_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    ticket: Mapped["Ticket"] = relationship(back_populates="runs", foreign_keys=[ticket_id])



class TicketWorkspace(Base):
    __tablename__ = "ticket_workspaces"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String, default="linked", nullable=False)
    label: Mapped[str] = mapped_column(String, default="", nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    access: Mapped[str] = mapped_column(String, default="read_write", nullable=False)
    source_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolved_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    repo_root: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    git_common_dir: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    relative_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    worktree_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    worktree_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    base_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    base_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    head_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    retention: Mapped[str] = mapped_column(String, default="preserve", nullable=False)
    state: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    ticket: Mapped["Ticket"] = relationship(back_populates="workspaces")

class ConfigRow(Base):
    __tablename__ = "config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    window_start: Mapped[str] = mapped_column(String, default="22:00")
    window_end: Mapped[str] = mapped_column(String, default="07:00")
    max_parallel: Mapped[int] = mapped_column(Integer, default=2)
    worktree_root: Mapped[str] = mapped_column(String)
    transcript_root: Mapped[str] = mapped_column(String)
    # Global default base ref for git_worktree tickets. When set, new tickets
    # created without an explicit base_ref branch from this ref instead of HEAD.
    worktree_base_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    polling_interval_seconds: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_run_duration_seconds: Mapped[int] = mapped_column(Integer, default=86400, nullable=False)
    run_token_grace_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    claude_binary_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    session_cookie_ttl_seconds: Mapped[int] = mapped_column(
        Integer, default=60 * 60 * 24 * 30, nullable=False,
    )
    cc_minimum_version: Mapped[str] = mapped_column(String, default="2.1.80", nullable=False)
    # Webhook URL for run-completion notifications (Slack/Discord/ntfy etc.).
    # Best-effort POST on run -> review transition; empty/NULL means disabled.
    notify_webhook_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Global IANA timezone for evaluating ScheduleWindow rows. Window start/end
    # are wall-clock times in this zone and day_mask bits are local weekdays;
    # the scheduler converts ``now`` into this zone before matching. Default
    # "UTC" reproduces the pre-timezone-fix behavior.
    schedule_timezone: Mapped[str] = mapped_column(String, default="UTC", nullable=False)


class ScheduleWindow(Base):
    """A named time window with its own parallelism cap.

    Replaces the single ``window_start``/``window_end``/``max_parallel`` triple
    on ConfigRow with a list of windows. The scheduler unions all matching
    windows for the current time/day and uses the most permissive cap.

    ``day_mask`` is a bitmask: Mon=1 Tue=2 Wed=4 Thu=8 Fri=16 Sat=32 Sun=64.
    Bit ``i`` corresponds to ``1 << datetime.weekday()`` (Mon=0..Sun=6). The
    default 127 (0b1111111) is every day.

    ``start``/``end`` are HH:MM strings with the same wraparound semantics as
    ``scheduler.in_window`` (equal start/end means always on; start > end wraps
    past midnight).
    """

    __tablename__ = "schedule_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String, default="", nullable=False)
    day_mask: Mapped[int] = mapped_column(Integer, default=127, nullable=False)
    start: Mapped[str] = mapped_column(String, default="00:00", nullable=False)
    end: Mapped[str] = mapped_column(String, default="00:00", nullable=False)
    max_parallel: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class RunToken(Base):
    __tablename__ = "run_tokens"

    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), nullable=False, index=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    scope_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DaemonStatus(Base):
    __tablename__ = "daemon_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cc_binary_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cc_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cc_check_status: Mapped[str] = mapped_column(String, default="unknown", nullable=False)
    cc_check_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String)
    pid: Mapped[int] = mapped_column(Integer)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CronJob(Base):
    """A recurring ticket template plus a schedule.

    When the schedule fires the worker materializes one ordinary
    ``status="queued"`` ticket from this template; that ticket then flows
    through the normal scheduler (active-hours window and ``max_parallel``
    capacity). Set ``force_run=True`` to instead materialize ``run_now=True``
    tickets, which the scheduler dispatches unconditionally — past a full queue
    and outside the active-hours window (overflow above ``max_parallel``).

    Backend neutrality: the agent backend is chosen by the referenced
    ``Profile.backend``, never by the cron job. This template stores only
    ``profile_id`` and no engine field — a profile pointed at a non-Claude
    backend produces tickets that run on that backend with zero cron changes.

    Workspace is directory-only in v1: ``workspace_mode`` is constrained to
    ``directory``/``in_place`` and generated tickets run in ``cwd`` with no
    worktree.
    """

    __tablename__ = "cron_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    # --- ticket template ---
    title: Mapped[str] = mapped_column(String, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cwd: Mapped[str] = mapped_column(String, nullable=False)
    # Constrained to 'directory' | 'in_place' on the API surface (no worktrees).
    workspace_mode: Mapped[str] = mapped_column(String, default="directory", nullable=False)
    additional_dirs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    permission_overrides: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # When true, generated tickets are created with run_now=True so the
    # scheduler dispatches them unconditionally (ignores active-hours window
    # and max_parallel capacity). Pairs with overlap_policy to avoid stacking.
    force_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # --- schedule ---
    # Standard 5-field cron expression: "minute hour dom month dow".
    schedule: Mapped[str] = mapped_column(String, nullable=False)
    # IANA timezone name (zoneinfo). Next-fire is computed in this tz; all DB
    # datetimes are stored in UTC.
    timezone: Mapped[str] = mapped_column(String, default="UTC", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 'coalesce' (default): one ticket for the oldest due fire, then advance past
    # now. No backfill in v1.
    misfire_policy: Mapped[str] = mapped_column(String, default="coalesce", nullable=False)
    # 'skip_if_active' (default): skip a new ticket if the previous generated
    # ticket is draft/queued/running. 'review' does not block.
    overlap_policy: Mapped[str] = mapped_column(String, default="skip_if_active", nullable=False)
    # Nullable while disabled (stale past value is harmless; the materialization
    # query filters enabled). UTC.
    next_fire_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fire_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ticket_id: Mapped[Optional[str]] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    fires: Mapped[list["CronJobFire"]] = relationship(
        back_populates="cron_job", cascade="all, delete-orphan",
    )


class CronJobFire(Base):
    """Idempotency + audit row for a single (cron_job, fire_at) materialization.

    The unique constraint on ``(cron_job_id, fire_at)`` makes materialization
    idempotent: a second tick that tries to claim the same fire hits the
    constraint and skips. ``ticket_id`` is NULL when the fire was skipped
    (e.g. overlap), with the reason recorded in ``skipped_reason`` so a
    suppressed run is auditable rather than a silent gap.
    """

    __tablename__ = "cron_job_fires"
    __table_args__ = (
        UniqueConstraint("cron_job_id", "fire_at", name="uq_cron_job_fires_job_fire"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    cron_job_id: Mapped[str] = mapped_column(ForeignKey("cron_jobs.id"), nullable=False, index=True)
    # The scheduled fire instant (UTC). Minute-granular for scheduled fires,
    # sub-minute for manual fire-now.
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ticket_id: Mapped[Optional[str]] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    # NULL when a ticket was generated; set (e.g. "overlap") when the fire was
    # recorded but suppressed.
    skipped_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    cron_job: Mapped["CronJob"] = relationship(back_populates="fires")
