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
    execution_target: Literal["local", "k8s"] = "local"
    endpoint_id: Optional[str] = None
    backend_config: dict = {}
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
    execution_target: Optional[Literal["local", "k8s"]] = None
    endpoint_id: Optional[str] = None
    backend_config: Optional[dict] = None
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
    execution_target: str = "local"
    endpoint_id: Optional[str] = None
    backend_config: dict = {}
    claude_credentials: Optional[ClaudeCredentialsOut] = None
    claude_binary_path: Optional[str] = None
    env_keys: list[str] = []
    system_prompt: Optional[str] = None
    permission_mode: Optional[str] = None
    cc_settings_passthrough: dict = {}
    run_token_scopes: list[str] = []
    # Non-blocking save-time notices (e.g. a model assignment outside its
    # endpoint's model menu). Empty when there is nothing to warn about.
    warnings: list[str] = []
    created_at: datetime
    updated_at: datetime


# --- Providers & endpoints -------------------------------------------------
#
# See docs/design/providers-and-endpoints.md ("Layer 1"). A Provider is a
# vendor identity; each ProviderEndpoint it owns carries its own protocol,
# base URL, credential, credential source, optional harness lock, and model
# menu. Credential/extra fields are write-only: requests carry
# `credential_value` / `extra`, responses expose only `credential_set` /
# `extra_set` booleans, never the plaintext.


class EndpointCreate(BaseModel):
    label: str = ""
    protocol_kind: str
    base_url: Optional[str] = None
    credential_source: str = "api_key"
    credential_value: Optional[str] = None
    harness_lock: Optional[str] = None
    default_model: Optional[str] = None
    models: list[str] = []
    extra: Optional[dict] = None


class EndpointUpdate(BaseModel):
    label: Optional[str] = None
    protocol_kind: Optional[str] = None
    base_url: Optional[str] = None
    credential_source: Optional[str] = None
    credential_value: Optional[str] = None
    harness_lock: Optional[str] = None
    default_model: Optional[str] = None
    models: Optional[list[str]] = None
    extra: Optional[dict] = None


class EndpointOut(BaseModel):
    id: str
    provider_id: str
    label: str
    protocol_kind: str
    base_url: Optional[str] = None
    credential_source: str
    credential_set: bool = False
    harness_lock: Optional[str] = None
    default_model: Optional[str] = None
    models: list[str] = []
    models_pulled_at: Optional[datetime] = None
    extra_set: bool = False
    created_at: datetime
    updated_at: datetime


class ProviderCreate(BaseModel):
    name: str
    vendor: str
    # Create-flow nesting: endpoints to seed alongside the provider.
    endpoints: list[EndpointCreate] = []
    # Seeded into every nested endpoint above whose own `credential_value` is
    # unset and whose `credential_source` is "api_key" (the ZAI convenience:
    # paste the key once, it lands on every selected endpoint).
    credential_value: Optional[str] = None


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    vendor: Optional[str] = None


class ProviderOut(BaseModel):
    id: str
    name: str
    vendor: str
    endpoints: list[EndpointOut] = []
    created_at: datetime
    updated_at: datetime


class ProviderRotateCredential(BaseModel):
    credential_value: str


class ProviderRotateResult(BaseModel):
    rotated: int


class CatalogEndpointOut(BaseModel):
    label: str
    protocol_kind: str
    base_url: Optional[str] = None
    harness_lock: Optional[str] = None
    default_model: Optional[str] = None


class CatalogOfferingOut(BaseModel):
    key: str
    label: str
    vendor: str
    credential_source: str
    credential_hint: Optional[str] = None
    description: str = ""
    suggested_name: str = ""
    endpoints: list[CatalogEndpointOut] = []


# --- Backends (Layer 2: harness capability catalog) ------------------------
#
# Mirrors ``nightdesk.domain.backend_capabilities.BackendCapability`` so the
# profile editor can render backend choice, field-group visibility, and model
# slots from data instead of a hard-coded list. See
# ``docs/design/providers-and-endpoints.md`` ("Layer 2: Harnesses").


class ModelSlotOut(BaseModel):
    name: str
    label: str
    required: bool = False


class BackendOut(BaseModel):
    code: str
    label: str
    summary: str
    protocol_kinds: list[str] = []
    multi_endpoint: bool = False
    requires_provider: bool = False
    enabled: bool = True
    executable: bool = True
    group_keys: list[str] = []
    model_slots: list[ModelSlotOut] = []
    capabilities: list[str] = []
    # Hard yes/no on whether the harness binary is actually present, so the
    # UI never has to guess from an "auto" placeholder. None for backends
    # without a runtime binary to probe (e.g. dummy). See BackendRuntimeOut.
    runtime: Optional["BackendRuntimeOut"] = None


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


def _normalize_source_path(v: object) -> str:
    """Expand ``~`` and require an absolute path. Empty values are rejected."""
    if not isinstance(v, str) or not v.strip():
        raise ValueError("source_path is required")
    p = os.path.expanduser(v.strip())
    if not p.startswith("/"):
        raise ValueError("source_path must be absolute (start with '/')")
    return p


def _normalize_source_path_optional(v: object) -> Optional[str]:
    """PATCH-friendly variant: ``None`` means 'leave alone'; empty string is rejected."""
    if v is None:
        return None
    return _normalize_source_path(v)




from nightdesk.domain.toolchains import clean_string_list as _clean_string_list


class ToolchainOverrides(BaseModel):
    enable: list[str] = Field(default_factory=list)
    disable: list[str] = Field(default_factory=list)
    extra_paths: list[str] = Field(default_factory=list)

    @field_validator("enable", "disable", "extra_paths", mode="before")
    @classmethod
    def _clean_list(cls, v: object, info):
        return _clean_string_list(v, field=info.field_name)

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



class ProjectCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    source_path: str
    default_workspace_mode: Optional[Literal["directory", "git_worktree", "in_place", "worktree"]] = None
    default_worktree_name_template: Optional[str] = None
    default_base_ref: Optional[str] = None
    default_linked_workspaces: Optional[list[TicketWorkspaceIn]] = None
    default_toolchains: list[str] = Field(default_factory=list)
    default_tool_paths: list[str] = Field(default_factory=list)
    color: Optional[str] = None
    position: int = 0

    @field_validator("source_path", mode="before")
    @classmethod
    def _source_path_abs(cls, v):
        return _normalize_source_path(v)

    @field_validator("default_toolchains", "default_tool_paths", mode="before")
    @classmethod
    def _tool_defaults(cls, v: object, info):
        return _clean_string_list(v, field=info.field_name)


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    source_path: Optional[str] = None
    default_workspace_mode: Optional[Literal["directory", "git_worktree", "in_place", "worktree"]] = None
    default_worktree_name_template: Optional[str] = None
    default_base_ref: Optional[str] = None
    default_linked_workspaces: Optional[list[TicketWorkspaceIn]] = None
    default_toolchains: Optional[list[str]] = None
    default_tool_paths: Optional[list[str]] = None
    color: Optional[str] = None
    position: Optional[int] = None

    @field_validator("source_path", mode="before")
    @classmethod
    def _source_path_abs(cls, v):
        return _normalize_source_path_optional(v)

    @field_validator("default_toolchains", "default_tool_paths", mode="before")
    @classmethod
    def _tool_defaults(cls, v: object, info):
        if v is None:
            return None
        return _clean_string_list(v, field=info.field_name)


class ProjectOut(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    slug: str
    source_path: str
    default_workspace_mode: Optional[str] = None
    default_worktree_name_template: Optional[str] = None
    default_base_ref: Optional[str] = None
    default_linked_workspaces: Optional[list[dict]] = None
    default_toolchains: list[str] = Field(default_factory=list)
    default_tool_paths: list[str] = Field(default_factory=list)
    color: Optional[str] = None
    position: int
    archived_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

class TicketCreate(BaseModel):
    title: str
    prompt: str = ""
    status: Optional[str] = None  # defaults to 'draft' server-side
    priority: int = 0
    # Required for every status except "inbox" (enforced server-side in
    # domain.tickets.create_ticket, mirroring the primary-workspace
    # exception below): a captured-but-under-specified inbox item may have
    # no profile yet; ``ticket_completeness`` blocks it from being promoted
    # until one is set.
    profile_id: Optional[str] = None
    project_id: Optional[str] = None
    permission_overrides: Optional[dict] = None
    toolchain_overrides: Optional[ToolchainOverrides] = None
    additional_dirs: list[AdditionalDir] = []
    source_path: Optional[str] = None
    workspace_mode: Literal["directory", "git_worktree", "in_place", "worktree"] = "directory"
    worktree_name: Optional[str] = None
    worktree_path: Optional[str] = None
    workspaces: Optional[list[TicketWorkspaceIn]] = None
    run_now: bool = False
    # Opt-in: commit this ticket's working-tree changes to its git_worktree
    # branch on run success so stacked/dependent tickets base_ref-pointing at
    # this branch actually receive the work. See nightdesk-ticket-ops.
    commit_on_finish: Optional[bool] = None
    scheduled_after: Optional[datetime] = None

    @field_validator("source_path", mode="before")
    @classmethod
    def _source_path_abs(cls, v):
        return _normalize_source_path_optional(v)

    @field_validator("priority")
    @classmethod
    def _valid_priority(cls, v: int) -> int:
        if v < 0 or v > 4:
            raise ValueError("priority must be between 0 and 4")
        return v



class TicketUpdate(BaseModel):
    title: Optional[str] = None
    prompt: Optional[str] = None
    priority: Optional[int] = None
    profile_id: Optional[str] = None
    project_id: Optional[str] = None
    permission_overrides: Optional[dict] = None
    toolchain_overrides: Optional[ToolchainOverrides] = None
    additional_dirs: Optional[list[AdditionalDir]] = None
    source_path: Optional[str] = None
    workspace_mode: Optional[Literal["directory", "git_worktree", "in_place", "worktree"]] = None
    worktree_name: Optional[str] = None
    worktree_path: Optional[str] = None
    workspaces: Optional[list[TicketWorkspaceIn]] = None
    run_now: Optional[bool] = None
    commit_on_finish: Optional[bool] = None
    scheduled_after: Optional[datetime] = None

    @field_validator("source_path", mode="before")
    @classmethod
    def _source_path_abs(cls, v):
        return _normalize_source_path_optional(v)

    @field_validator("priority")
    @classmethod
    def _valid_priority(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and (v < 0 or v > 4):
            raise ValueError("priority must be between 0 and 4")
        return v



class LabelOut(BaseModel):
    id: str
    name: str
    color: str


class TicketOut(BaseModel):
    id: str
    title: str
    prompt: str
    status: str
    priority: int
    position: int
    project_id: Optional[str] = None
    profile_id: Optional[str] = None
    permission_overrides: Optional[dict] = None
    toolchain_overrides: Optional[dict] = None
    additional_dirs: list[AdditionalDir] = []
    workspaces: list[TicketWorkspaceOut] = []
    labels: list[LabelOut] = []
    run_now: bool
    commit_on_finish: Optional[bool] = None
    scheduled_after: Optional[datetime] = None
    current_run_id: Optional[str] = None
    next_run_context: Optional[str] = None
    next_run_context_updated_at: Optional[datetime] = None
    dependencies: list[DependencyOut] = []
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
    sandbox_tool_paths: Optional[list[str]] = None


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


class ScheduleWindowsReplace(BaseModel):
    """Full replacement set for the windows editor's atomic save."""
    timezone: str = "UTC"
    windows: list[ScheduleWindowCreate] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            raise ValueError(f"unknown timezone: {v}")
        return v


class ConfigOut(BaseModel):
    window_start: str
    window_end: str
    max_parallel: int
    worktree_root: str
    transcript_root: str
    worktree_base_ref: Optional[str] = None
    notify_webhook_url: Optional[str] = None
    schedule_timezone: str = "UTC"
    windows: list[ScheduleWindowOut] = Field(default_factory=list)
    toolchain_presets: dict[str, list[str]] = Field(default_factory=dict)
    # Harness-global runtime paths (Layer 2). Empty/unset means auto-discover
    # from PATH (and, for opencode, ~/.opencode/bin).
    claude_binary_path: Optional[str] = None
    opencode_binary_path: Optional[str] = None
    # Cloud sandbox (Kubernetes executor). All optional; a k8s profile is only
    # runnable once a runner image + cluster-routable API address are set.
    k8s_kubeconfig_path: Optional[str] = None
    k8s_in_cluster: bool = False
    k8s_namespace: str = "nightdesk"
    k8s_runner_image: Optional[str] = None
    k8s_cpu_request: Optional[str] = None
    k8s_cpu_limit: Optional[str] = None
    k8s_mem_request: Optional[str] = None
    k8s_mem_limit: Optional[str] = None
    k8s_node_selector: dict[str, str] = Field(default_factory=dict)
    k8s_runtime_class: Optional[str] = None
    k8s_git_credentials_secret: Optional[str] = None

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
    # Webhook URL for run-completion notifications. Empty string clears it.
    notify_webhook_url: Optional[str] = None
    schedule_timezone: Optional[str] = None
    toolchain_presets: Optional[dict[str, list[str]]] = None
    # Empty string clears the override (falls back to PATH discovery).
    claude_binary_path: Optional[str] = None
    opencode_binary_path: Optional[str] = None
    # Cloud sandbox (Kubernetes executor). Empty string clears a string field.
    k8s_kubeconfig_path: Optional[str] = None
    k8s_in_cluster: Optional[bool] = None
    k8s_namespace: Optional[str] = None
    k8s_runner_image: Optional[str] = None
    k8s_cpu_request: Optional[str] = None
    k8s_cpu_limit: Optional[str] = None
    k8s_mem_request: Optional[str] = None
    k8s_mem_limit: Optional[str] = None
    k8s_node_selector: Optional[dict[str, str]] = None
    k8s_runtime_class: Optional[str] = None
    k8s_git_credentials_secret: Optional[str] = None

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

    @field_validator("notify_webhook_url", mode="before")
    @classmethod
    def _clean_webhook_url(cls, v: object) -> object:
        if v is None:
            return v
        if not isinstance(v, str):
            raise ValueError("notify_webhook_url must be a string")
        return v.strip()


    @field_validator("toolchain_presets", mode="before")
    @classmethod
    def _clean_toolchain_presets(cls, v: object) -> object:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("toolchain_presets must be an object")
        return {
            str(name).strip(): _clean_string_list(paths, field=f"toolchain_presets.{name}")
            for name, paths in v.items()
            if str(name).strip()
        }
    @field_validator("schedule_timezone")
    @classmethod
    def _validate_schedule_tz(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            raise ValueError(f"unknown timezone: {v}")
        return v


class WorkerStatusOut(BaseModel):
    host: Optional[str] = None
    pid: Optional[int] = None
    last_seen_at: Optional[datetime] = None
    stale: bool = False
    in_window: bool = False
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    max_parallel: int = 0
    active_window: Optional[str] = None
    schedule_timezone: str = "UTC"
    normal_running: int = 0
    run_now_running: int = 0
    total_running: int = 0
    # Legacy field retained so older clients keep working.
    running_count: int = 0
    # Live completed-run spend estimate for the header chip / worker pill.
    day_spend_usd: float = 0.0
    month_spend_usd: float = 0.0


class SearchHit(BaseModel):
    id: str
    title: str
    snippet: str
    status: str
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    project_color: Optional[str] = None


class DependencyOut(BaseModel):
    id: str
    ticket_id: str
    depends_on_id: str
    depends_on_title: str
    depends_on_status: str
    created_at: datetime


class DependencyCreate(BaseModel):
    depends_on_id: str


# --- Focused metadata update schemas -------------------------------------------
# Lightweight payloads for the property picker, list inline edits, keyboard
# actions, and bulk operations.  Each schema targets exactly one ticket field
# so callers don't need to construct a full TicketUpdate just to change the
# priority.  Bulk variants accept a list of ticket IDs and return a
# BulkUpdateResult with per-ticket success/skip details.


class TicketPriorityUpdate(BaseModel):
    """Sparse priority update using the fixed 0..4 metadata scale."""

    priority: int

    @field_validator("priority")
    @classmethod
    def _valid_priority(cls, v: int) -> int:
        if v < 0 or v > 4:
            raise ValueError("priority must be between 0 and 4")
        return v


class TicketStatusUpdate(BaseModel):
    """Focused status transition.  Respects the ticket lifecycle state machine;
    the domain layer rejects invalid transitions with InvalidTransition."""

    status: str

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in _LIFECYCLE_STATUSES:
            raise ValueError(
                f"status must be one of {_LIFECYCLE_STATUSES}, got {v!r}"
            )
        return v


class TicketProjectUpdate(BaseModel):
    """Sparse project assignment.  Pass ``null`` to clear."""

    project_id: Optional[str] = None


class TicketProfileUpdate(BaseModel):
    """Sparse profile reassignment.  The new profile must exist."""

    profile_id: str


class TicketContinue(BaseModel):
    """Follow-up message to continue a ticket's ACTIVE conversation.

    The message becomes the next user turn on the resumed SDK session (same
    runtime, full prior message history). Mirrors the HTMX ``/continue`` form's
    ``next_run_context`` field, which is why it maps to ``next_run_context``
    internally. Empty/whitespace is rejected (422) — there is nothing to append.
    """

    message: str

    @field_validator("message")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be empty")
        return v


class TicketNewConversation(BaseModel):
    """Start a fresh conversation (new session, no resumed history).

    Mirrors the HTMX ``/new-conversation`` form. ``workspace`` is ``"keep"``
    (reuse the current worktree files) or ``"fresh"`` (fresh worktree path).
    ``profile_id`` switches the runtime for the NEXT new conversation only —
    switching runtime always starts a new conversation because sessions are not
    portable across runtimes.
    """

    message: Optional[str] = None
    profile_id: Optional[str] = None
    workspace: Literal["keep", "fresh"] = "keep"

    @field_validator("message")
    @classmethod
    def _clean_message(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            return None
        return v


class BulkPriorityUpdate(BaseModel):
    ticket_ids: list[str] = Field(min_length=1)
    priority: int

    @field_validator("priority")
    @classmethod
    def _valid_priority(cls, v: int) -> int:
        if v < 0 or v > 4:
            raise ValueError("priority must be between 0 and 4")
        return v


class BulkStatusUpdate(BaseModel):
    ticket_ids: list[str] = Field(min_length=1)
    status: str

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in _LIFECYCLE_STATUSES:
            raise ValueError(
                f"status must be one of {_LIFECYCLE_STATUSES}, got {v!r}"
            )
        return v


class BulkProjectUpdate(BaseModel):
    ticket_ids: list[str] = Field(min_length=1)
    project_id: Optional[str] = None


class BulkProfileUpdate(BaseModel):
    ticket_ids: list[str] = Field(min_length=1)
    profile_id: str


class BulkUpdateResult(BaseModel):
    """Result of a bulk metadata update.  ``updated`` holds the tickets that
    were changed; ``skipped`` lists tickets that could not be updated (not
    found, invalid transition, etc.) with a human-readable reason."""

    updated: list[TicketOut]
    skipped: list[dict]


# --- Cron jobs ----------------------------------------------------------------

# Cron is directory-only in v1. The API accepts 'directory'/'in_place' and
# rejects worktree modes (the domain layer enforces this too, with 422).
CronWorkspaceMode = Literal["directory", "in_place"]


class CronJobCreate(BaseModel):
    title: str
    prompt: str = ""
    profile_id: str
    source_path: str
    # 5-field cron expression: "minute hour day-of-month month day-of-week".
    schedule: str
    # IANA timezone name; JSON API defaults to UTC.
    timezone: str = "UTC"
    priority: int = 0
    workspace_mode: CronWorkspaceMode = "directory"
    additional_dirs: list[AdditionalDir] = []
    permission_overrides: Optional[dict] = None
    enabled: bool = True
    # When true, generated tickets are run_now=True (dispatched past the queue
    # and outside the active-hours window).
    force_run: bool = False
    misfire_policy: Literal["coalesce"] = "coalesce"
    overlap_policy: Literal["skip_if_active", "always"] = "skip_if_active"

    @field_validator("source_path", mode="before")
    @classmethod
    def _source_path_abs(cls, v):
        return _normalize_source_path(v)

    @field_validator("priority")
    @classmethod
    def _valid_priority(cls, v: int) -> int:
        if v < 0 or v > 4:
            raise ValueError("priority must be between 0 and 4")
        return v


class CronJobUpdate(BaseModel):
    title: Optional[str] = None
    prompt: Optional[str] = None
    profile_id: Optional[str] = None
    source_path: Optional[str] = None
    schedule: Optional[str] = None
    timezone: Optional[str] = None
    priority: Optional[int] = None
    workspace_mode: Optional[CronWorkspaceMode] = None
    additional_dirs: Optional[list[AdditionalDir]] = None
    permission_overrides: Optional[dict] = None
    force_run: Optional[bool] = None
    misfire_policy: Optional[Literal["coalesce"]] = None
    overlap_policy: Optional[Literal["skip_if_active", "always"]] = None

    @field_validator("source_path", mode="before")
    @classmethod
    def _source_path_abs(cls, v):
        return _normalize_source_path_optional(v)

    @field_validator("priority")
    @classmethod
    def _valid_priority(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and (v < 0 or v > 4):
            raise ValueError("priority must be between 0 and 4")
        return v


class CronJobOut(BaseModel):
    id: str
    title: str
    prompt: str
    profile_id: str
    source_path: str
    schedule: str
    timezone: str
    priority: int
    workspace_mode: str
    additional_dirs: list[AdditionalDir] = []
    permission_overrides: Optional[dict] = None
    enabled: bool
    force_run: bool = False
    misfire_policy: str
    overlap_policy: str
    next_fire_at: Optional[datetime] = None
    last_fire_at: Optional[datetime] = None
    last_ticket_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# --- Inbox ----------------------------------------------------------------


class InboxItemOut(BaseModel):
    """An inbox ticket plus the human-readable reasons it isn't yet
    promotable (empty ``blockers`` means the ticket can be promoted)."""

    ticket: TicketOut
    blockers: list[str] = []


class TicketPromote(BaseModel):
    """Promote an inbox item onto the runnable board."""

    target: Literal["draft", "queued"] = "draft"


class InboxCountOut(BaseModel):
    count: int


# --- Saved views ------------------------------------------------------------


class SavedViewCreate(BaseModel):
    name: str
    surface: str
    params: dict[str, str] = {}


class SavedViewUpdate(BaseModel):
    """Rename a saved view. ``params``/``surface`` are immutable after
    creation (mirrors the HTMX rename-only affordance)."""

    name: str


class SavedViewReorder(BaseModel):
    view_ids: list[str] = Field(min_length=1)


class SavedViewOut(BaseModel):
    id: str
    name: str
    surface: str
    params: dict
    url: str


# --- Conversation / run actions ----------------------------------------------


class TicketNextRunContext(BaseModel):
    """Free-form steering note staged for the ticket's next run."""

    body: str = ""


class TicketRestart(BaseModel):
    message: Optional[str] = None
    workspace_policy: Literal["recreate_in_place", "fresh_path"]


class TicketResumeOrRetry(BaseModel):
    message: Optional[str] = None


class TicketClone(BaseModel):
    title: Optional[str] = None
    carry_context: bool = False


class AdditionalDirAdd(BaseModel):
    path: str
    mode: Literal["rw", "ro"] = "rw"

    @field_validator("path")
    @classmethod
    def _absolute(cls, v: str) -> str:
        if not v.strip().startswith("/"):
            raise ValueError("path must be absolute")
        return v.strip()


# --- Bulk labels --------------------------------------------------------------


class BulkLabelsUpdate(BaseModel):
    ticket_ids: list[str] = Field(min_length=1)
    label_ids: list[str] = []


class BulkArchiveRequest(BaseModel):
    ticket_ids: list[str] = Field(min_length=1)


# --- Profiles: copy/export/import --------------------------------------------


class ProfileImport(BaseModel):
    """Native nightdesk profile export re-imported as JSON (no file upload).

    Mirrors the multipart ``POST /profiles/import`` HTML action; secrets are
    never accepted here beyond what ``claude_credentials``/``env`` explicitly
    carry, and forbidden fields are silently stripped exactly as the HTML
    importer does.
    """

    name: Optional[str] = None
    payload: dict = Field(default_factory=dict)


class ProfileImportFromCC(BaseModel):
    """A Claude Code ``settings.json`` payload to translate into a profile."""

    settings: dict = Field(default_factory=dict)
    name: Optional[str] = None


class ProfileImportResult(BaseModel):
    id: str
    dropped_fields: list[str] = []


# --- Helpers -------------------------------------------------------------------


class WorktreeNamePreviewRequest(BaseModel):
    source_path: str
    name: Optional[str] = None
    path: Optional[str] = None
    base_ref: Optional[str] = None


class WorktreeNamePreviewOut(BaseModel):
    path: str
    source: str
    base_ref: Optional[str] = None
    base_ref_status: Optional[str] = None


class CronPreviewRequest(BaseModel):
    schedule: str
    timezone: str = "UTC"
    count: int = Field(default=5, ge=1, le=20)


class CronPreviewOut(BaseModel):
    next_fire_times: list[datetime]


class WebhookTestRequest(BaseModel):
    url: str


class ProjectActivityRow(BaseModel):
    run_id: str
    ticket_id: str
    ticket_title: str
    outcome: str
    duration_seconds: Optional[float] = None
    tokens: Optional[int] = None
    started_at: Optional[datetime] = None


class AnalyticsSummaryOut(BaseModel):
    """JSON twin of the headline chips on ``/analytics``."""

    price_source: str
    price_as_of: str
    price_source_label: str
    today: dict
    last_7d: dict
    last_30d: dict
    run_stats: dict
    duration: dict
    # Per-project rollup (spend, tokens, run count, success rate) over the
    # same rolling 30-day window as the other breakdowns. When ``project_id``
    # is passed this degenerates to that one project's own row.
    by_project: list[dict] = []


class AnalyticsSpendOut(BaseModel):
    range: Literal["today", "7d", "30d"]
    project_id: Optional[str] = None
    totals: dict
    by_model: list[dict]
    by_profile: list[dict]
    by_ticket: list[dict]
    by_project: list[dict] = []
    # Per-day {date, cost, run_count, ...token breakdown} for the requested
    # range — the same series ``tokens`` returns, so a spend-over-time chart
    # doesn't need a second round trip.
    daily_series: list[dict]
    price_source: str
    price_as_of: str


class AnalyticsTokensOut(BaseModel):
    range: Literal["today", "7d", "30d"]
    project_id: Optional[str] = None
    by_model: list[dict]
    model_legend: list[dict]
    # Per-day {date, total_tokens, input_tokens, output_tokens,
    # cache_read_tokens, cache_write_tokens, cost, run_count, by_model}.
    daily_series: list[dict]
    max_daily_tokens: int


class AnalyticsLatencyOut(BaseModel):
    range: Literal["today", "7d", "30d"]
    project_id: Optional[str] = None
    latency_by_model: list[dict]
    latency_series: list[dict]
    latency_model_legend: list[dict]
    max_daily_latency: float
    model_vs_tool_time: list[dict]


class DiagnosticsOut(BaseModel):
    nightdesk_version: str
    python_version: str
    platform: str
    kernel: str
    bwrap_version: Optional[str] = None
    cc_check_status: Optional[str] = None
    cc_version: Optional[str] = None
    cc_binary_path: Optional[str] = None
    cc_check_message: Optional[str] = None


class BackendRuntimeOut(BaseModel):
    """Hard yes/no on whether a harness binary is present. See BackendOut.runtime."""

    binary_path_override: Optional[str] = None
    resolved_path: Optional[str] = None
    source: Optional[Literal["override", "path", "default"]] = None
    found: bool = False
    version: Optional[str] = None
