# src/nightdesk/domain/permissions.py
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional


# Claude Code Bash permission rules that block `git push` (and force-push, which
# is just `git push --force …`). These are injected into the sandboxed agent's
# disallowed-tools set UNLESS the run opts in via ``allow_git_push``. The goal is
# to stop a sandboxed agent from accidentally pushing to (and destroying) a real
# remote — the sandbox shares the host network namespace, so without this gate a
# stray ``git push --force`` would reach origin.
#
# Both forms are needed: ``Bash(git push)`` matches the bare command and
# ``Bash(git push:*)`` matches any command that starts with ``git push`` (so
# ``git push origin main``, ``git push --force``, etc. are all covered).
GIT_PUSH_DENY_RULES = (
    "Bash(git push)",
    "Bash(git push:*)",
)


@dataclass(frozen=True)
class PermissionSpec:
    fs_read: list[str] = field(default_factory=list)
    fs_write: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    # v1 accepts "off" | "on". Legacy "none"/"localhost"/"allowlist" are migrated.
    network_mode: str = "off"
    network_allowlist: list[str] = field(default_factory=list)
    secret_keys: list[str] = field(default_factory=list)
    tool_paths: list[str] = field(default_factory=list)
    default_model: Optional[str] = None
    backend: str = "claude_sdk"
    # Backend-specific configuration forwarded from Profile.backend_config.
    # Opaque to the worker; each backend reads the keys it understands.
    backend_config: dict = field(default_factory=dict)
    # Resolved at preflight from Profile.claude_credentials (decrypted).
    # Shape: {"source": "inherit"|"api_key"|"auth_token", "value": "..."}
    # `inherit` value is the absolute host path to .credentials.json.
    claude_credentials: Optional[dict] = None
    # Decrypted custom env vars from Profile.env (kv dict).
    custom_env: dict = field(default_factory=dict)
    # Absolute host path to the resolved claude binary for this run.
    claude_binary_path: Optional[str] = None
    permission_mode: Optional[str] = None
    system_prompt: Optional[str] = None
    # Destructive-remote gate. ``git push`` is DISALLOWED by default (see
    # GIT_PUSH_DENY_RULES); set this True (e.g. ticket permission override
    # ``{"allow_git_push": true}``) to let the sandboxed agent push.
    allow_git_push: bool = False


_ADDITIVE = ("fs_read", "fs_write", "allowed_tools", "network_allowlist", "secret_keys", "tool_paths")
_RESTRICTIVE = ("denied_tools",)
_REPLACE = ("network_mode", "default_model", "backend", "backend_config", "permission_mode", "allow_git_push")


def _dedup(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def merge_permissions(profile: PermissionSpec, override: dict | None) -> PermissionSpec:
    if not override:
        return replace(profile)
    new_fields: dict = {}
    for key in _ADDITIVE:
        merged = list(getattr(profile, key)) + list(override.get(key, []))
        new_fields[key] = _dedup(merged)
    for key in _RESTRICTIVE:
        merged = list(getattr(profile, key)) + list(override.get(key, []))
        new_fields[key] = _dedup(merged)
    for key in _REPLACE:
        if key in override and override[key] is not None:
            new_fields[key] = override[key]
        else:
            new_fields[key] = getattr(profile, key)
    return replace(profile, **new_fields)
