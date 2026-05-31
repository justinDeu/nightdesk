from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping, Optional

from sqlalchemy.orm import Session

from nightdesk.db.models import ConfigRow, Project, Ticket


BUILTIN_TOOLCHAINS: dict[str, list[str]] = {
    "user-python-tools": ["~/.local/bin"],
    "rust-user-tools": ["~/.cargo/bin"],
}

BUILTIN_TOOLCHAIN_METADATA: dict[str, dict[str, str]] = {
    "user-python-tools": {
        "label": "User local tools",
        "description": "Any user-installed executables available in ~/.local/bin.",
    },
    "rust-user-tools": {
        "label": "Cargo-installed tools",
        "description": "Tools installed by cargo install.",
    },
}


# Canonical singleton primary key for ConfigRow. There is only ever one config
# row in the database; centralized here so callers don't repeat the magic id.
_CONFIG_ROW_ID = 1


def current_config(session: Session) -> Optional[ConfigRow]:
    return session.get(ConfigRow, _CONFIG_ROW_ID)


def clean_string_list(values: object, *, field: str) -> list[str]:
    """Strict normalizer for user-supplied string lists.

    Strips, drops empties, dedupes (order-preserving), and rejects non-list /
    non-string entries and NUL bytes. Use at input boundaries (Pydantic
    validators, domain create/update). For defensive parsing of already-stored
    data, use ``_resolve_clean`` instead.
    """
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError(f"{field} must be a list")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{field} entries must be strings")
        item = value.strip()
        if not item:
            continue
        if "\x00" in item:
            raise ValueError(f"{field} entries must not contain NUL")
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _resolve_clean(values: object) -> list[str]:
    """Lenient version of clean_string_list for already-stored data.

    Workers must not crash on a corrupt row. Silently drops non-string entries
    and NUL bytes instead of raising.
    """
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


# Host paths that must never be visible inside the sandbox. Mirrors the
# defensive check in nightdesk.worker.sandbox.assert_no_excluded_paths so we
# can reject obviously bad input at create/update time rather than at run
# dispatch time. Kept narrow and home-relative on purpose.
def _exclusion_paths() -> list[Path]:
    home = Path(os.path.expanduser("~"))
    return [
        home / ".config" / "nightdesk",
        home / ".local" / "share" / "nightdesk",
        home / ".claude",
    ]


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def assert_paths_not_excluded(paths: Iterable[str], *, field: str) -> None:
    """Reject absolute paths that overlap protected nightdesk directories.

    Relative paths are not checked: they are resolved against a per-ticket
    workspace base_path at run time and can only be evaluated then. The
    worker still re-checks resolved paths via sandbox.assert_no_excluded_paths
    as a belt-and-suspenders guard.
    """
    excluded = [p.resolve() for p in _exclusion_paths()]
    for raw in paths:
        if not raw:
            continue
        expanded = os.path.expanduser(raw)
        if not os.path.isabs(expanded):
            continue
        rp = Path(expanded).resolve()
        for ex in excluded:
            if _is_under(rp, ex) or _is_under(ex, rp):
                raise ValueError(
                    f"{field}: path {raw!r} overlaps protected directory {str(ex)!r}"
                )


def _resolve_path(value: str, *, base_path: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(value))
    if os.path.isabs(expanded):
        return str(Path(expanded))
    return str((Path(base_path) / expanded).resolve(strict=False))


def _presets(config: Optional[ConfigRow]) -> dict[str, list[str]]:
    presets = {name: list(paths) for name, paths in BUILTIN_TOOLCHAINS.items()}
    configured = getattr(config, "toolchain_presets", None) or {}
    if isinstance(configured, Mapping):
        for name, paths in configured.items():
            clean_name = str(name).strip()
            if clean_name:
                presets[clean_name] = _resolve_clean(paths)
    return presets


def toolchain_presets(config: Optional[ConfigRow]) -> dict[str, list[str]]:
    return _presets(config)


def toolchain_names(config: Optional[ConfigRow]) -> list[str]:
    return sorted(_presets(config))



def toolchain_options(config: Optional[ConfigRow]) -> list[dict[str, object]]:
    presets = _presets(config)
    options: list[dict[str, object]] = []
    for name in sorted(presets):
        metadata = BUILTIN_TOOLCHAIN_METADATA.get(name, {})
        options.append({
            "name": name,
            "label": metadata.get("label") or name,
            "description": metadata.get("description") or "Custom toolchain preset.",
            "paths": presets[name],
            "builtin": name in BUILTIN_TOOLCHAINS,
        })
    return options

def assert_known_toolchains(names: Iterable[str], *, config: Optional[ConfigRow]) -> None:
    """Validate that every name in ``names`` is a known preset.

    Callers should pass only the names whose existence is load-bearing — the
    'enable' override and project defaults. Disabling an unknown name is a
    runtime no-op (filtered from ``selected``), so disable lists should NOT
    be validated here.
    """
    known = set(toolchain_names(config))
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(f"unknown toolchain: {', '.join(unknown)}")


def resolve_tool_paths(
    *,
    ticket: Ticket,
    config: Optional[ConfigRow],
    base_path: str,
) -> list[str]:
    """Resolve project defaults plus ticket overrides into host directories.

    Global config defines named recipes. A project chooses defaults. A ticket may
    enable or disable recipe names for one run and add extra directories. The
    sandbox only receives concrete absolute directories.
    """
    project: Optional[Project] = ticket.project
    selected = _resolve_clean(getattr(project, "default_toolchains", None)) if project else []
    raw_paths = _resolve_clean(getattr(project, "default_tool_paths", None)) if project else []

    overrides = ticket.toolchain_overrides or {}
    if not isinstance(overrides, dict):
        overrides = {}
    disabled = set(_resolve_clean(overrides.get("disable")))
    selected = [name for name in selected if name not in disabled]
    for name in _resolve_clean(overrides.get("enable")):
        if name not in disabled and name not in selected:
            selected.append(name)
    raw_paths.extend(_resolve_clean(overrides.get("extra_paths")))

    presets = _presets(config)
    for name in selected:
        raw_paths.extend(presets.get(name, []))

    seen: set[str] = set()
    out: list[str] = []
    for path in raw_paths:
        resolved = _resolve_path(path, base_path=base_path)
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out
