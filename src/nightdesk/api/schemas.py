from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ClaudeCredentialsIn(BaseModel):
    """Inbound credentials shape: `{source, value?, base_url?}`.

    - source='inherit' uses the user's `~/.claude/.credentials.json`.
    - source='api_key' sets `ANTHROPIC_API_KEY` from value.
    - source='auth_token' sets `ANTHROPIC_AUTH_TOKEN` from value.

    The value is encrypted before being written to the database. Missing
    value with an env-based source means "leave the existing secret alone"
    (PATCH semantics for rotation). `base_url` is optional and rides
    alongside any source — used to point CC at a non-Anthropic endpoint.
    """

    source: Literal["inherit", "api_key", "auth_token"]
    value: Optional[str] = None
    base_url: Optional[str] = None


class ClaudeCredentialsOut(BaseModel):
    """Outbound shape never returns the plaintext. `value_set` is True iff a
    secret is on record."""

    source: Literal["inherit", "api_key", "auth_token"]
    value_set: bool = False
    base_url: Optional[str] = None


class ProfileCreate(BaseModel):
    name: str
    description: str = ""
    fs_read: list[str] = []
    fs_write: list[str] = []
    allowed_tools: list[str] = []
    denied_tools: list[str] = []
    network_mode: str = "off"
    network_allowlist: list[str] = []
    secret_keys: list[str] = []
    default_model: Optional[str] = None
    backend: str = "claude_sdk"
    claude_credentials: Optional[ClaudeCredentialsIn] = None
    claude_binary_path: Optional[str] = None
    env: Optional[dict[str, str]] = None
    system_prompt: Optional[str] = None
    permission_mode: Optional[Literal["default", "acceptEdits", "bypassPermissions"]] = None
    cc_settings_passthrough: dict = {}
    run_token_scopes: list[str] = []


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    fs_read: Optional[list[str]] = None
    fs_write: Optional[list[str]] = None
    allowed_tools: Optional[list[str]] = None
    denied_tools: Optional[list[str]] = None
    network_mode: Optional[str] = None
    network_allowlist: Optional[list[str]] = None
    secret_keys: Optional[list[str]] = None
    default_model: Optional[str] = None
    backend: Optional[str] = None
    claude_credentials: Optional[ClaudeCredentialsIn] = None
    claude_binary_path: Optional[str] = None
    env: Optional[dict[str, str]] = None
    system_prompt: Optional[str] = None
    permission_mode: Optional[Literal["default", "acceptEdits", "bypassPermissions"]] = None
    cc_settings_passthrough: Optional[dict] = None
    run_token_scopes: Optional[list[str]] = None


class ProfileOut(BaseModel):
    id: str
    name: str
    description: str = ""
    fs_read: list[str]
    fs_write: list[str]
    allowed_tools: list[str]
    denied_tools: list[str]
    network_mode: str
    network_allowlist: list[str]
    secret_keys: list[str]
    default_model: Optional[str] = None
    backend: str = "claude_sdk"
    claude_credentials: Optional[ClaudeCredentialsOut] = None
    claude_binary_path: Optional[str] = None
    env_keys: list[str] = []
    system_prompt: Optional[str] = None
    permission_mode: Optional[str] = None
    cc_settings_passthrough: dict = {}
    run_token_scopes: list[str] = []
    created_at: datetime
    updated_at: datetime



class TicketWorkspaceIn(BaseModel):
    role: Literal["primary", "linked"] = "linked"
    label: str = ""
    kind: Literal["directory", "git_worktree", "in_place", "worktree"]
    access: Literal["read_write", "read_only"] = "read_write"
    source_path: Optional[str] = None
    worktree_name: Optional[str] = None
    worktree_path: Optional[str] = None
    branch: Optional[str] = None
    base_ref: Optional[str] = None
    retention: Literal["preserve", "cleanup_on_success", "cleanup_after_review"] = "preserve"

    @field_validator("source_path", "worktree_path", mode="before")
    @classmethod
    def _abs_path_optional(cls, v: object) -> Optional[str]:
        if v is None or v == "":
            return None
        if not isinstance(v, str):
            raise ValueError("path must be a string")
        p = os.path.expanduser(v.strip())
        if not p.startswith("/"):
            raise ValueError("path must be absolute (start with '/')")
        return p


class AdditionalDir(BaseModel):
    """A per-ticket extra directory to expose to the sandbox.

    In v2 only ``mode='rw'`` is honored; ``mode='ro'`` is parsed but ignored
    by the worker (warned at runtime). Paths must be absolute.
    """

    path: str
    mode: Literal["rw", "ro"] = "rw"

    @field_validator("path")
    @classmethod
    def _abs_path(cls, v: str) -> str:
        if not isinstance(v, str) or not v.startswith("/"):
            raise ValueError("path must be absolute (start with '/')")
        return v


def _normalize_cwd(v: object) -> str:
    """Expand ``~`` and require an absolute path. Empty values are rejected.

    Shared by ``TicketCreate`` (required) and ``TicketUpdate`` (only when
    the caller actually sends a value — see ``_normalize_cwd_optional``).
    """
    if not isinstance(v, str) or not v.strip():
        raise ValueError("cwd is required")
    p = os.path.expanduser(v.strip())
    if not p.startswith("/"):
        raise ValueError("cwd must be absolute (start with '/')")
    return p


def _normalize_cwd_optional(v: object) -> Optional[str]:
    """PATCH-friendly variant: ``None`` means 'leave alone'; empty string is rejected."""
    if v is None:
        return None
    return _normalize_cwd(v)



class TicketWorkspaceOut(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    ticket_id: str
    run_id: Optional[str] = None
    role: str
    label: str
    kind: str
    access: str
    source_path: Optional[str] = None
    resolved_path: Optional[str] = None
    repo_root: Optional[str] = None
    git_common_dir: Optional[str] = None
    relative_path: Optional[str] = None
    worktree_name: Optional[str] = None
    worktree_path: Optional[str] = None
    branch: Optional[str] = None
    base_ref: Optional[str] = None
    base_sha: Optional[str] = None
    head_sha: Optional[str] = None
    retention: str
    state: str
    position: int

class TicketCreate(BaseModel):
    title: str
    prompt: str = ""
    status: Optional[str] = None  # defaults to 'draft' server-side
    priority: int = 0
    profile_id: str
    permission_overrides: Optional[dict] = None
    additional_dirs: list[AdditionalDir] = []
    cwd: Optional[str] = None
    workspace_mode: Literal["directory", "git_worktree", "in_place", "worktree"] = "directory"
    worktree_name: Optional[str] = None
    worktree_path: Optional[str] = None
    workspaces: Optional[list[TicketWorkspaceIn]] = None
    run_now: bool = False
    scheduled_after: Optional[datetime] = None

    @field_validator("cwd", mode="before")
    @classmethod
    def _cwd_abs(cls, v):
        return _normalize_cwd_optional(v)


class TicketUpdate(BaseModel):
    title: Optional[str] = None
    prompt: Optional[str] = None
    priority: Optional[int] = None
    profile_id: Optional[str] = None
    permission_overrides: Optional[dict] = None
    additional_dirs: Optional[list[AdditionalDir]] = None
    cwd: Optional[str] = None
    workspace_mode: Optional[Literal["directory", "git_worktree", "in_place", "worktree"]] = None
    worktree_name: Optional[str] = None
    worktree_path: Optional[str] = None
    workspaces: Optional[list[TicketWorkspaceIn]] = None
    run_now: Optional[bool] = None
    scheduled_after: Optional[datetime] = None

    @field_validator("cwd", mode="before")
    @classmethod
    def _cwd_abs(cls, v):
        return _normalize_cwd_optional(v)


class TicketOut(BaseModel):
    id: str
    title: str
    prompt: str
    status: str
    priority: int
    position: int
    profile_id: str
    permission_overrides: Optional[dict] = None
    additional_dirs: list[AdditionalDir] = []
    cwd: str
    workspace_mode: str = "directory"
    worktree_name: Optional[str] = None
    worktree_path: Optional[str] = None
    workspaces: list[TicketWorkspaceOut] = []
    run_now: bool
    scheduled_after: Optional[datetime] = None
    current_run_id: Optional[str] = None
    next_run_context: Optional[str] = None
    next_run_context_updated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


_LIFECYCLE_STATUSES = ("draft", "queued", "running", "review", "archived")


class TicketTransition(BaseModel):
    status: str
    position: Optional[int] = None

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in _LIFECYCLE_STATUSES:
            raise ValueError(
                f"status must be one of {_LIFECYCLE_STATUSES}, got {v!r}"
            )
        return v


class TicketReorder(BaseModel):
    status: str
    ticket_ids: list[str]

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in _LIFECYCLE_STATUSES:
            raise ValueError(
                f"status must be one of {_LIFECYCLE_STATUSES}, got {v!r}"
            )
        return v


class RunOut(BaseModel):
    id: str
    ticket_id: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    exit_status: Optional[str] = None
    error_summary: Optional[str] = None
    worktree_path: str
    transcript_path: str
    pid: Optional[int] = None
    host: str
    started_as_run_now: bool = False
    intent: str = "first_run"
    parent_run_id: Optional[str] = None
    headless_policy_version: Optional[str] = None
    restart_workspace_policy: Optional[str] = None
    failure_kind: Optional[str] = None
    model_used: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    cost_usd: Optional[float] = None


_HH_MM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

# day_mask is a 7-bit field: Mon=1 Tue=2 Wed=4 Thu=8 Fri=16 Sat=32 Sun=64.
_DAY_MASK_ALL = 127


class ScheduleWindowOut(BaseModel):
    id: int
    label: str
    day_mask: int
    start: str
    end: str
    max_parallel: int
    position: int

    model_config = {"from_attributes": True}


class ScheduleWindowCreate(BaseModel):
    label: str = ""
    day_mask: int = _DAY_MASK_ALL
    start: str = "00:00"
    end: str = "00:00"
    max_parallel: int = 1
    position: int = 0

    @field_validator("start", "end", mode="before")
    @classmethod
    def _validate_hh_mm(cls, v: object) -> object:
        if not isinstance(v, str) or not _HH_MM_RE.match(v):
            raise ValueError("must be a valid HH:MM time (00:00-23:59)")
        return v

    @field_validator("day_mask")
    @classmethod
    def _validate_day_mask(cls, v: int) -> int:
        if v < 0 or v > _DAY_MASK_ALL:
            raise ValueError(f"day_mask must be between 0 and {_DAY_MASK_ALL}")
        return v

    @field_validator("max_parallel")
    @classmethod
    def _validate_max_parallel(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_parallel must be >= 0")
        return v


class ScheduleWindowUpdate(BaseModel):
    """Sparse PATCH payload. Only provided fields are applied."""

    label: Optional[str] = None
    day_mask: Optional[int] = None
    start: Optional[str] = None
    end: Optional[str] = None
    max_parallel: Optional[int] = None
    position: Optional[int] = None

    @field_validator("start", "end", mode="before")
    @classmethod
    def _validate_hh_mm(cls, v: object) -> object:
        if v is None:
            return v
        if not isinstance(v, str) or not _HH_MM_RE.match(v):
            raise ValueError("must be a valid HH:MM time (00:00-23:59)")
        return v

    @field_validator("day_mask")
    @classmethod
    def _validate_day_mask(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        if v < 0 or v > _DAY_MASK_ALL:
            raise ValueError(f"day_mask must be between 0 and {_DAY_MASK_ALL}")
        return v

    @field_validator("max_parallel")
    @classmethod
    def _validate_max_parallel(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        if v < 0:
            raise ValueError("max_parallel must be >= 0")
        return v


class ConfigOut(BaseModel):
    window_start: str
    window_end: str
    max_parallel: int
    worktree_root: str
    transcript_root: str
    worktree_base_ref: Optional[str] = None
    windows: list[ScheduleWindowOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ConfigUpdate(BaseModel):
    """Mutable runtime config fields.

    ``worktree_root`` and ``transcript_root`` are intentionally excluded.
    They are bootstrap-only and must be set in ``~/.config/nightdesk/config.toml``
    before launch. Changing data-dir paths at runtime would require a migration
    and is not supported in v1.
    """

    window_start: Optional[str] = None
    window_end: Optional[str] = None
    max_parallel: Optional[int] = None
    # Global default base ref for git_worktree tickets. Pass an empty string to
    # clear the default (tickets then branch from HEAD). None leaves it alone.
    worktree_base_ref: Optional[str] = None

    @field_validator("window_start", "window_end", mode="before")
    @classmethod
    def validate_hh_mm(cls, v: object) -> object:
        if v is None:
            return v
        if not isinstance(v, str) or not _HH_MM_RE.match(v):
            raise ValueError("must be a valid HH:MM time (00:00-23:59)")
        return v

    @field_validator("worktree_base_ref", mode="before")
    @classmethod
    def _strip_base_ref(cls, v: object) -> object:
        if v is None:
            return v
        if not isinstance(v, str):
            raise ValueError("worktree_base_ref must be a string")
        return v.strip()


class WorkerStatusOut(BaseModel):
    host: Optional[str] = None
    pid: Optional[int] = None
    last_seen_at: Optional[datetime] = None
    stale: bool = False
    in_window: bool = False
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    max_parallel: int = 0
    normal_running: int = 0
    run_now_running: int = 0
    total_running: int = 0
    # Legacy field retained so older clients keep working.
    running_count: int = 0


class SearchHit(BaseModel):
    id: str
    title: str
    snippet: str
    status: str
