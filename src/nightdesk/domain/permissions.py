# src/nightdesk/domain/permissions.py
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional


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
    default_model: Optional[str] = None
    backend: str = "claude_sdk"
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


_ADDITIVE = ("fs_read", "fs_write", "allowed_tools", "network_allowlist", "secret_keys")
_RESTRICTIVE = ("denied_tools",)
_REPLACE = ("network_mode", "default_model", "backend", "permission_mode")


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
