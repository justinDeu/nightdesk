"""Effective execution-context resolver with provenance.

This is the single source of truth for "what would this ticket actually run
with?". The worker assembles the same picture imperatively at dispatch time
(``run_one._profile_to_spec`` + ``merge_permissions`` + ``resolve_tool_paths``
+ workspace prep); this module reproduces the *merge semantics* in a pure,
side-effect-free way so every preview surface can show the result before a run
exists.

Every resolved value carries a ``provenance`` tag naming the layer responsible
for it:

* ``global``   -- global config row (``ConfigRow``)
* ``project``  -- project default
* ``profile``  -- the selected profile
* ``ticket``   -- a ticket-level override
* ``derived``  -- behavior derived from the workspace / sandbox at run time
                  (e.g. the working dir becoming a writable mount, the git-push
                  deny rules, worktree branch naming)

Merge semantics mirror :mod:`nightdesk.domain.permissions`:

* additive list fields (fs reach, tools, allowlists, secret keys, tool paths)
  concatenate ``profile`` then ``ticket`` then ``derived`` and dedupe,
  first-source-wins;
* replace fields (network mode, model, backend, permission mode) take the
  ticket override when present, otherwise the profile value.

Validation is intentionally shallow: obvious config / path / reference issues
only. This resolver never claims to predict whether a run will succeed.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from nightdesk.db.models import ConfigRow, Profile, Project, Ticket
from nightdesk.domain.permissions import (
    GIT_PUSH_DENY_RULES,
    PermissionSpec,
    merge_permissions,
)
from nightdesk.domain.profiles import _CC_PERMISSION_MODES, get_profile
from nightdesk.domain.projects import apply_project_defaults, get_project
from nightdesk.domain.toolchains import (
    current_config,
    resolve_tool_paths,
    toolchain_names,
)


def _available_backends() -> list[str]:
    """Lazy import: keep the worker/SDK out of the API import path."""
    try:
        from nightdesk.worker.backends import available_backends

        return available_backends()
    except Exception:
        return ["claude_sdk", "dummy"]


# --- provenance vocabulary --------------------------------------------------

GLOBAL = "global"
PROJECT = "project"
PROFILE = "profile"
TICKET = "ticket"
DERIVED = "derived"

PROVENANCE_LABELS: dict[str, str] = {
    GLOBAL: "Global config",
    PROJECT: "Project default",
    PROFILE: "Profile",
    TICKET: "Ticket override",
    DERIVED: "Derived",
}


# --- result data model ------------------------------------------------------


@dataclass(frozen=True)
class Contribution:
    """One entry in a merged list value, tagged with where it came from."""

    value: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {"value": self.value, "source": self.source}


@dataclass(frozen=True)
class ConfigField:
    """A single resolved configuration value.

    ``value`` holds a scalar display value (str / bool / None). ``items`` holds
    the per-entry contributions for list-valued fields. A field uses one or the
    other, never both meaningfully.
    """

    key: str
    label: str
    source: str
    value: Any = None
    items: tuple[Contribution, ...] = ()
    note: Optional[str] = None

    @property
    def source_label(self) -> str:
        return PROVENANCE_LABELS.get(self.source, self.source)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "source": self.source,
            "source_label": self.source_label,
            "value": self.value,
            "items": [c.as_dict() for c in self.items],
            "note": self.note,
        }


@dataclass(frozen=True)
class ConfigGroup:
    title: str
    fields: tuple[ConfigField, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "fields": [f.as_dict() for f in self.fields]}


@dataclass(frozen=True)
class EffectiveConfig:
    groups: tuple[ConfigGroup, ...]
    issues: tuple[str, ...]

    def field_map(self) -> dict[str, ConfigField]:
        """Flatten to ``{key: field}`` for ergonomic assertions/lookups."""
        out: dict[str, ConfigField] = {}
        for group in self.groups:
            for fld in group.fields:
                out[fld.key] = fld
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "groups": [g.as_dict() for g in self.groups],
            "issues": list(self.issues),
        }


# --- normalized input -------------------------------------------------------


@dataclass
class ResolveInput:
    """Layer inputs normalized from either a stored ticket or a draft dict."""

    profile: Optional[Profile]
    project: Optional[Project]
    config: Optional[ConfigRow]
    permission_overrides: Optional[dict]
    toolchain_overrides: Optional[dict]
    additional_dirs: list
    workspaces: list[dict]
    # Convenience handles to the primary workspace.
    source_path: Optional[str]
    workspace_mode: Optional[str]
    base_ref: Optional[str]


def _clean_list(values: object) -> list[str]:
    """Lenient string-list normalizer (mirrors toolchains._resolve_clean)."""
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip()
        if not item or "\x00" in item:
            continue
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _ws_from_row(w) -> dict:
    return {
        "role": getattr(w, "role", "linked"),
        "label": getattr(w, "label", ""),
        "kind": getattr(w, "kind", "directory"),
        "access": getattr(w, "access", "read_write"),
        "source_path": getattr(w, "source_path", None),
        "worktree_name": getattr(w, "worktree_name", None),
        "worktree_path": getattr(w, "worktree_path", None),
        "base_ref": getattr(w, "base_ref", None),
    }


def _ws_from_dict(d: dict) -> dict:
    return {
        "role": d.get("role", "linked"),
        "label": d.get("label", ""),
        "kind": d.get("kind", "directory"),
        "access": d.get("access", "read_write"),
        "source_path": d.get("source_path"),
        "worktree_name": d.get("worktree_name"),
        "worktree_path": d.get("worktree_path"),
        "base_ref": d.get("base_ref"),
    }


def _primary(workspaces: list[dict]) -> Optional[dict]:
    for w in workspaces:
        if w.get("role") == "primary":
            return w
    return workspaces[0] if workspaces else None


def input_from_ticket(session: Session, ticket: Ticket) -> ResolveInput:
    workspaces = [_ws_from_row(w) for w in (ticket.workspaces or [])]
    primary = _primary(workspaces)
    return ResolveInput(
        profile=ticket.profile,
        project=ticket.project,
        config=current_config(session),
        permission_overrides=ticket.permission_overrides,
        toolchain_overrides=ticket.toolchain_overrides,
        additional_dirs=list(ticket.additional_dirs or []),
        workspaces=workspaces,
        source_path=(primary or {}).get("source_path"),
        workspace_mode=(primary or {}).get("kind"),
        base_ref=(primary or {}).get("base_ref"),
    )


def input_from_draft(session: Session, fields: dict[str, Any]) -> ResolveInput:
    """Normalize a draft ticket dict (ticket preview / promote-from-inbox).

    Runs ``apply_project_defaults`` so the preview reflects exactly what
    ``create_ticket`` would persist: project default workspaces, workspace
    mode, and toolchain enable-set are all filled in here.
    """
    merged = apply_project_defaults(session, dict(fields))

    profile: Optional[Profile] = None
    profile_id = merged.get("profile_id")
    if profile_id:
        try:
            profile = get_profile(session, profile_id)
        except Exception:
            profile = None

    project: Optional[Project] = None
    project_id = merged.get("project_id")
    if project_id:
        try:
            project = get_project(session, project_id)
        except Exception:
            project = None

    raw_workspaces = merged.get("workspaces")
    workspaces: list[dict] = []
    if isinstance(raw_workspaces, list):
        workspaces = [_ws_from_dict(w) for w in raw_workspaces if isinstance(w, dict)]
    if not workspaces and merged.get("source_path"):
        workspaces = [{
            "role": "primary",
            "label": "primary",
            "kind": merged.get("workspace_mode") or "directory",
            "access": "read_write",
            "source_path": merged.get("source_path"),
            "worktree_name": merged.get("worktree_name"),
            "worktree_path": merged.get("worktree_path"),
            "base_ref": merged.get("base_ref"),
        }]

    primary = _primary(workspaces)
    return ResolveInput(
        profile=profile,
        project=project,
        config=current_config(session),
        permission_overrides=merged.get("permission_overrides"),
        toolchain_overrides=merged.get("toolchain_overrides"),
        additional_dirs=list(merged.get("additional_dirs") or []),
        workspaces=workspaces,
        source_path=(primary or {}).get("source_path") or merged.get("source_path"),
        workspace_mode=(primary or {}).get("kind") or merged.get("workspace_mode"),
        base_ref=(primary or {}).get("base_ref"),
    )


# --- merge helpers ----------------------------------------------------------


def _merge_items(
    *groups: tuple[str, list[str]],
) -> tuple[Contribution, ...]:
    """Concatenate ``(source, values)`` groups, dedupe first-source-wins."""
    seen: set[str] = set()
    out: list[Contribution] = []
    for source, values in groups:
        for v in values:
            if v in seen:
                continue
            seen.add(v)
            out.append(Contribution(value=v, source=source))
    return tuple(out)


def _replace_field(
    key: str,
    label: str,
    profile_value: Any,
    overrides: Optional[dict],
    *,
    default_display: Optional[str] = None,
) -> ConfigField:
    """Replace-semantics scalar: ticket override wins when present & non-null."""
    if overrides and key in overrides and overrides[key] is not None:
        value = overrides[key]
        source = TICKET
    else:
        value = profile_value
        source = PROFILE
    display = value
    if value in (None, ""):
        display = default_display
    return ConfigField(key=key, label=label, source=source, value=display)


# --- workspace-derived reach ------------------------------------------------


def _workspace_reach(inp: ResolveInput) -> tuple[list[str], list[str]]:
    """Derived fs reach the sandbox grants for the workspaces themselves.

    The worker mounts every read_write workspace into ``fs_write`` and every
    read_only workspace into ``fs_read`` (``run_one._apply_workspace_permissions``).
    For a directory workspace the mounted path is the source path; for a
    git_worktree the *future* worktree path isn't known until a run creates it,
    so we fall back to the source path with a note rather than inventing one.
    """
    writes: list[str] = []
    reads: list[str] = []
    for w in inp.workspaces:
        path = w.get("worktree_path") or w.get("source_path")
        if not path:
            continue
        if w.get("access") == "read_only":
            reads.append(str(path))
        else:
            writes.append(str(path))
    # Ticket additional_dirs become extra workspaces with rw/ro access.
    for entry in inp.additional_dirs:
        if not isinstance(entry, dict):
            continue
        p = entry.get("path")
        if not isinstance(p, str) or not p:
            continue
        if entry.get("mode") == "ro":
            reads.append(p)
        else:
            writes.append(p)
    return writes, reads


def _git_meta_derived_writes(inp: ResolveInput) -> list[str]:
    """Best-effort git metadata dirs bound rw for git_worktree workspaces.

    Mirrors ``run_one._git_metadata_dirs``: for each git_worktree workspace,
    we run ``git rev-parse --git-common-dir`` against the source path to
    discover the repo's common git directory (covers both regular and
    bare/.bare layouts). Falls back gracefully when git is unavailable or
    the path is not a git repo.
    """
    dirs: list[str] = []
    seen: set[str] = set()
    for w in inp.workspaces:
        if w.get("kind") not in ("git_worktree", "worktree"):
            continue
        source = w.get("source_path")
        if not isinstance(source, str) or not source:
            continue
        try:
            r = subprocess.run(
                ["git", "-C", source, "rev-parse", "--git-common-dir"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                continue
            gcd = r.stdout.strip()
            if not gcd:
                continue
            if not os.path.isabs(gcd):
                gcd = str(Path(source) / gcd)
            resolved = str(Path(gcd).resolve())
            if resolved not in seen:
                seen.add(resolved)
                dirs.append(resolved)
        except Exception:
            pass
    return dirs


def _additional_dir_items(inp: ResolveInput, mode: str) -> list[str]:
    out: list[str] = []
    for entry in inp.additional_dirs:
        if not isinstance(entry, dict):
            continue
        p = entry.get("path")
        if not isinstance(p, str) or not p:
            continue
        if entry.get("mode", "rw") == mode:
            out.append(p)
    return out


# --- toolchain resolution with provenance -----------------------------------


def _resolve_toolchains(inp: ResolveInput) -> tuple[Contribution, ...]:
    """Selected toolchain preset names with project/ticket provenance."""
    project_defaults = _clean_list(
        getattr(inp.project, "default_toolchains", None) if inp.project else None
    )
    overrides = inp.toolchain_overrides if isinstance(inp.toolchain_overrides, dict) else {}
    disabled = set(_clean_list(overrides.get("disable")))
    enabled = _clean_list(overrides.get("enable"))

    out: list[Contribution] = []
    seen: set[str] = set()
    for name in project_defaults:
        if name in disabled or name in seen:
            continue
        seen.add(name)
        out.append(Contribution(value=name, source=PROJECT))
    for name in enabled:
        if name in disabled or name in seen:
            continue
        seen.add(name)
        out.append(Contribution(value=name, source=TICKET))
    return tuple(out)


# --- validation (shallow) ---------------------------------------------------

_PROTECTED_HINT = ("/.config/nightdesk", "/.local/share/nightdesk", "/.claude")


def _path_overlaps_protected(raw: str) -> bool:
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        return False
    try:
        rp = str(Path(expanded).resolve())
    except OSError:
        return False
    return any(hint in rp for hint in _PROTECTED_HINT)


def _collect_issues(inp: ResolveInput, spec: PermissionSpec) -> list[str]:
    issues: list[str] = []

    if inp.profile is None:
        issues.append("No profile selected — a run cannot be dispatched.")

    backend = spec.backend
    if backend and backend not in _available_backends():
        issues.append(
            f"Unknown backend {backend!r}; known backends: "
            f"{', '.join(_available_backends())}."
        )

    if spec.permission_mode and spec.permission_mode not in _CC_PERMISSION_MODES:
        issues.append(
            f"Unknown permission mode {spec.permission_mode!r}; "
            f"expected one of {', '.join(sorted(_CC_PERMISSION_MODES))}."
        )

    if not inp.source_path:
        issues.append("No primary workspace source path set.")
    elif not str(inp.source_path).startswith("/"):
        issues.append(
            f"Primary workspace source path {inp.source_path!r} is not absolute."
        )

    # Unknown toolchain references (load-bearing enable set + project defaults).
    known = set(toolchain_names(inp.config))
    referenced: list[str] = []
    overrides = inp.toolchain_overrides if isinstance(inp.toolchain_overrides, dict) else {}
    referenced.extend(_clean_list(overrides.get("enable")))
    if inp.project:
        referenced.extend(_clean_list(getattr(inp.project, "default_toolchains", None)))
    for name in referenced:
        if name not in known:
            issues.append(f"Unknown toolchain preset {name!r}.")

    # Credentials structure check (best-effort; blob is Fernet-encrypted in
    # production, so json.loads raises ValueError and we skip silently).
    _profile = inp.profile
    if _profile and getattr(_profile, "claude_credentials", None):
        import json as _json
        blob = _profile.claude_credentials
        try:
            creds_dict = _json.loads(blob) if isinstance(blob, str) else blob
            if isinstance(creds_dict, dict):
                cred_source = creds_dict.get("source")
                if not cred_source:
                    issues.append(
                        "Profile credentials blob is missing a 'source' field."
                    )
                elif cred_source in ("api_key", "auth_token"):
                    value = creds_dict.get("value")
                    if not value or (isinstance(value, str) and not value.strip()):
                        issues.append(
                            f"Profile credentials source={cred_source!r} has an empty value."
                        )
        except (ValueError, TypeError, AttributeError):
            pass

    # Blank entries in secret_keys are noise and likely misconfiguration.
    # Check raw lists (not cleaned) so blanks that survived storage are caught.
    raw_profile_keys = list(_profile.secret_keys or []) if _profile else []
    if any(isinstance(k, str) and not k.strip() for k in raw_profile_keys):
        issues.append("Profile secret_keys contains a blank entry.")
    raw_ovr_keys = list((inp.permission_overrides or {}).get("secret_keys") or [])
    if any(isinstance(k, str) and not k.strip() for k in raw_ovr_keys):
        issues.append("Ticket secret_keys override contains a blank entry.")

    # Path references overlapping protected nightdesk dirs.
    for label, paths in (
        ("fs_read", spec.fs_read),
        ("fs_write", spec.fs_write),
        ("tool path", _clean_list(overrides.get("extra_paths"))),
    ):
        for p in paths:
            if _path_overlaps_protected(p):
                issues.append(
                    f"{label} {p!r} overlaps a protected nightdesk directory."
                )

    # Dedupe while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for msg in issues:
        if msg not in seen:
            seen.add(msg)
            unique.append(msg)
    return unique


# --- public resolver --------------------------------------------------------


def resolve_effective_config(inp: ResolveInput) -> EffectiveConfig:
    """Resolve the merged execution context for a normalized input."""
    profile = inp.profile
    overrides = inp.permission_overrides

    # Build the profile baseline spec (no secrets — preview is read-only and
    # never decrypts credentials), then apply ticket permission overrides so
    # replace-semantics values resolve identically to the worker.
    if profile is not None:
        base = PermissionSpec(
            fs_read=list(profile.fs_read or []),
            fs_write=list(profile.fs_write or []),
            allowed_tools=list(profile.allowed_tools or []),
            denied_tools=list(profile.denied_tools or []),
            network_mode=profile.network_mode or "off",
            network_allowlist=list(profile.network_allowlist or []),
            secret_keys=list(profile.secret_keys or []),
            default_model=profile.default_model,
            backend=getattr(profile, "backend", None) or "claude_sdk",
            claude_binary_path=getattr(profile, "claude_binary_path", None),
            permission_mode=getattr(profile, "permission_mode", None),
            system_prompt=getattr(profile, "system_prompt", None),
        )
    else:
        base = PermissionSpec()
    spec = merge_permissions(base, overrides)

    ovr = overrides if isinstance(overrides, dict) else {}
    profile_fs_read = list(profile.fs_read or []) if profile else []
    profile_fs_write = list(profile.fs_write or []) if profile else []
    profile_allowed = list(profile.allowed_tools or []) if profile else []
    profile_denied = list(profile.denied_tools or []) if profile else []
    profile_netallow = list(profile.network_allowlist or []) if profile else []
    profile_secrets = list(profile.secret_keys or []) if profile else []

    ws_writes, ws_reads = _workspace_reach(inp)

    # --- group: Project & workspace ---------------------------------------
    primary_kind = inp.workspace_mode or "directory"
    project_mode = getattr(inp.project, "default_workspace_mode", None) if inp.project else None
    if project_mode and project_mode == primary_kind:
        mode_source = PROJECT
    elif inp.workspace_mode:
        mode_source = TICKET
    else:
        mode_source = DERIVED

    project_source_path = getattr(inp.project, "source_path", None) if inp.project else None
    if inp.project and inp.source_path == project_source_path:
        path_source = PROJECT
    elif inp.source_path:
        path_source = TICKET
    else:
        path_source = DERIVED

    project_field = ConfigField(
        key="project",
        label="Project",
        source=TICKET if inp.project else DERIVED,
        value=(inp.project.name if inp.project else "(none)"),
    )

    workspace_path_field = ConfigField(
        key="source_path",
        label="Workspace source path",
        source=path_source,
        value=inp.source_path or "(unset)",
    )

    workspace_mode_field = ConfigField(
        key="workspace_mode",
        label="Workspace mode",
        source=mode_source,
        value=primary_kind,
    )

    # git-worktree metadata behavior (derived). Only meaningful for worktrees.
    if primary_kind in ("git_worktree", "worktree"):
        project_base = getattr(inp.project, "default_base_ref", None) if inp.project else None
        global_base = getattr(inp.config, "worktree_base_ref", None) if inp.config else None
        base_ref = inp.base_ref or project_base or global_base
        worktree_root = getattr(inp.config, "worktree_root", None) if inp.config else None
        # The ticket workspace materializes the project/global default base_ref
        # at create time, so attribute by *origin*: a base_ref equal to the
        # project (or global) default is reported as coming from that layer,
        # not the ticket.
        if inp.base_ref and project_base and inp.base_ref == project_base:
            base_ref_src = PROJECT
        elif inp.base_ref and not project_base and global_base and inp.base_ref == global_base:
            base_ref_src = GLOBAL
        elif inp.base_ref:
            base_ref_src = TICKET
        elif project_base:
            base_ref_src = PROJECT
        elif global_base:
            base_ref_src = GLOBAL
        else:
            base_ref_src = DERIVED
        git_field = ConfigField(
            key="git_worktree",
            label="Git worktree",
            source=DERIVED,
            value="created at run time",
            note=(
                f"branch nightdesk/<name> from {base_ref or 'HEAD'}; "
                f"worktree root {worktree_root or '(default)'}; "
                "git metadata mounted read-write."
            ),
        )
        base_ref_field = ConfigField(
            key="base_ref",
            label="Base ref",
            source=base_ref_src,
            value=base_ref or "HEAD",
        )
        project_group = ConfigGroup(
            title="Project & workspace",
            fields=(project_field, workspace_path_field, workspace_mode_field,
                    base_ref_field, git_field),
        )
    else:
        dir_note = ConfigField(
            key="git_worktree",
            label="Git worktree",
            source=DERIVED,
            value="not used",
            note="directory mode: the agent runs in the source path in place.",
        )
        project_group = ConfigGroup(
            title="Project & workspace",
            fields=(project_field, workspace_path_field, workspace_mode_field, dir_note),
        )

    # --- group: Execution backend -----------------------------------------
    profile_field = ConfigField(
        key="profile",
        label="Profile",
        source=TICKET if profile else DERIVED,
        value=(profile.name if profile else "(none)"),
    )
    backend_field = _replace_field("backend", "Backend", base.backend, overrides)
    model_field = _replace_field(
        "default_model", "Model", base.default_model, overrides,
        default_display="(backend default)",
    )
    permission_mode_field = _replace_field(
        "permission_mode", "Permission mode", base.permission_mode, overrides,
        default_display="(default)",
    )
    if profile and getattr(profile, "claude_binary_path", None):
        cc_bin_value, cc_bin_src = profile.claude_binary_path, PROFILE
    elif inp.config and getattr(inp.config, "claude_binary_path", None):
        cc_bin_value, cc_bin_src = inp.config.claude_binary_path, GLOBAL
    else:
        cc_bin_value, cc_bin_src = "(resolved from PATH)", DERIVED
    claude_bin_field = ConfigField(
        key="claude_binary_path", label="Claude binary",
        source=cc_bin_src, value=cc_bin_value,
    )
    creds_configured = bool(profile and getattr(profile, "claude_credentials", None))
    credentials_field = ConfigField(
        key="credentials", label="Credentials",
        source=PROFILE if profile else DERIVED,
        value="configured" if creds_configured else "not set",
        note=None if creds_configured else "no Claude auth source configured on the profile.",
    )
    backend_group = ConfigGroup(
        title="Execution backend",
        fields=(profile_field, backend_field, model_field, permission_mode_field,
                claude_bin_field, credentials_field),
    )

    # --- group: Filesystem reach ------------------------------------------
    fs_read_field = ConfigField(
        key="fs_read", label="Filesystem read",
        source=PROFILE if profile_fs_read else DERIVED,
        items=_merge_items(
            (PROFILE, profile_fs_read),
            (TICKET, _clean_list(ovr.get("fs_read"))),
            (TICKET, _additional_dir_items(inp, "ro")),
            (DERIVED, ws_reads),
        ),
    )
    git_meta_writes = _git_meta_derived_writes(inp)
    fs_write_field = ConfigField(
        key="fs_write", label="Filesystem write",
        source=PROFILE if profile_fs_write else DERIVED,
        items=_merge_items(
            (PROFILE, profile_fs_write),
            (TICKET, _clean_list(ovr.get("fs_write"))),
            (TICKET, _additional_dir_items(inp, "rw")),
            (DERIVED, ws_writes),
            (DERIVED, git_meta_writes),
        ),
        note=(
            "Git common dir additionally mounted read-write for worktree operations."
            if git_meta_writes else None
        ),
    )
    fs_group = ConfigGroup(
        title="Filesystem reach",
        fields=(fs_read_field, fs_write_field),
    )

    # --- group: Tools ------------------------------------------------------
    allowed_items = _merge_items(
        (PROFILE, profile_allowed),
        (TICKET, _clean_list(ovr.get("allowed_tools"))),
    )
    # git-push deny rules are injected by the executor unless allow_git_push.
    push_rules = [] if spec.allow_git_push else list(GIT_PUSH_DENY_RULES)
    denied_items = _merge_items(
        (PROFILE, profile_denied),
        (TICKET, _clean_list(ovr.get("denied_tools"))),
        (DERIVED, push_rules),
    )
    allowed_field = ConfigField(
        key="allowed_tools", label="Allowed tools",
        source=PROFILE, items=allowed_items,
        note=None if allowed_items else "empty = all tools allowed unless denied.",
    )
    denied_field = ConfigField(
        key="denied_tools", label="Denied tools",
        source=DERIVED if (not profile_denied and push_rules) else PROFILE,
        items=denied_items,
        note=(
            "git push allowed by override." if spec.allow_git_push
            else "git push denied by default (set allow_git_push to opt in)."
        ),
    )
    tools_group = ConfigGroup(
        title="Tools",
        fields=(allowed_field, denied_field),
    )

    # --- group: Network & secrets -----------------------------------------
    _nm_field = _replace_field(
        "network_mode", "Network mode", base.network_mode, overrides,
    )
    _effective_nm = _nm_field.value
    network_mode_field = (
        ConfigField(
            key=_nm_field.key,
            label=_nm_field.label,
            source=_nm_field.source,
            value=_nm_field.value,
            note=(
                "Enforced via tool deny rules (Bash/WebFetch denied), not "
                "OS-level network isolation — the sandbox shares the host "
                "network namespace so inference can reach the Anthropic API."
            ),
        )
        if _effective_nm == "off"
        else _nm_field
    )
    network_allow_field = ConfigField(
        key="network_allowlist", label="Network allowlist",
        source=PROFILE if profile_netallow else DERIVED,
        items=_merge_items(
            (PROFILE, profile_netallow),
            (TICKET, _clean_list(ovr.get("network_allowlist"))),
        ),
    )
    secrets_field = ConfigField(
        key="secret_keys", label="Secret env keys",
        source=PROFILE if profile_secrets else DERIVED,
        items=_merge_items(
            (PROFILE, profile_secrets),
            (TICKET, _clean_list(ovr.get("secret_keys"))),
        ),
    )
    network_group = ConfigGroup(
        title="Network & secrets",
        fields=(network_mode_field, network_allow_field, secrets_field),
    )

    # --- group: Toolchain & PATH ------------------------------------------
    toolchain_items = _resolve_toolchains(inp)
    toolchains_field = ConfigField(
        key="toolchains", label="Toolchain presets",
        source=PROJECT if any(c.source == PROJECT for c in toolchain_items) else (
            TICKET if toolchain_items else DERIVED
        ),
        items=toolchain_items,
        note=None if toolchain_items else "no toolchain presets selected.",
    )
    # Extra literal paths (project defaults + ticket extra_paths), before
    # resolution against the workspace base.
    extra_path_items = _merge_items(
        (PROJECT, _clean_list(getattr(inp.project, "default_tool_paths", None)) if inp.project else []),
        (TICKET, _clean_list(ovr_toolchain_extra(inp))),
    )
    extra_paths_field = ConfigField(
        key="extra_paths", label="Extra tool paths",
        source=PROJECT if any(c.source == PROJECT for c in extra_path_items) else (
            TICKET if extra_path_items else DERIVED
        ),
        items=extra_path_items,
    )
    # Final resolved PATH dirs (presets expanded + paths resolved against the
    # workspace base). This is the concrete derived value the sandbox receives.
    resolved_paths: list[str] = []
    if profile is not None or inp.project is not None or inp.toolchain_overrides:
        resolved_paths = _safe_resolve_tool_paths(inp)
    resolved_field = ConfigField(
        key="resolved_tool_paths", label="Resolved PATH dirs",
        source=DERIVED,
        items=tuple(Contribution(value=p, source=DERIVED) for p in resolved_paths),
        note=(
            "Presets expanded and relative paths resolved against the workspace. "
            "Symlinked tool runtimes (e.g. pipx/cargo shims) are additionally "
            "mounted read-only at run time."
        ),
    )
    toolchain_group = ConfigGroup(
        title="Toolchain & PATH",
        fields=(toolchains_field, extra_paths_field, resolved_field),
    )

    groups = (
        project_group,
        backend_group,
        fs_group,
        tools_group,
        network_group,
        toolchain_group,
    )
    issues = _collect_issues(inp, spec)
    return EffectiveConfig(groups=groups, issues=tuple(issues))


def ovr_toolchain_extra(inp: ResolveInput) -> list:
    overrides = inp.toolchain_overrides if isinstance(inp.toolchain_overrides, dict) else {}
    return overrides.get("extra_paths") or []


def _safe_resolve_tool_paths(inp: ResolveInput) -> list[str]:
    """Resolve toolchains+paths to concrete dirs without touching the DB.

    Builds a throwaway ``Ticket``/``Project`` shaped object good enough for
    ``resolve_tool_paths`` (which only reads ``project.default_toolchains``,
    ``project.default_tool_paths``, and ``ticket.toolchain_overrides``).
    """
    base_path = inp.source_path or "/"

    class _P:
        default_toolchains = (
            list(getattr(inp.project, "default_toolchains", None) or [])
            if inp.project else []
        )
        default_tool_paths = (
            list(getattr(inp.project, "default_tool_paths", None) or [])
            if inp.project else []
        )

    class _T:
        project = _P()
        toolchain_overrides = inp.toolchain_overrides

    try:
        return resolve_tool_paths(ticket=_T(), config=inp.config, base_path=base_path)
    except Exception:
        return []


# --- convenience entry points ----------------------------------------------


def resolve_for_ticket(session: Session, ticket: Ticket) -> EffectiveConfig:
    return resolve_effective_config(input_from_ticket(session, ticket))


def resolve_for_draft(session: Session, fields: dict[str, Any]) -> EffectiveConfig:
    return resolve_effective_config(input_from_draft(session, fields))
