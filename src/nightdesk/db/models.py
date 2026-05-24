from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy import (
    String, Integer, Boolean, DateTime, ForeignKey, JSON, Text, Time,
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


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String)
    prompt: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="draft", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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

    profile: Mapped["Profile"] = relationship(lazy="joined")
    runs: Mapped[list["Run"]] = relationship(back_populates="ticket", foreign_keys="Run.ticket_id")
    workspaces: Mapped[list["TicketWorkspace"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketWorkspace.position",
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
