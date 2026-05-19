from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nightdesk.db.models import Profile


# Fields that must never round-trip through Nightdesk profiles. Imports
# strip them with a warning; the editor never exposes them. Hooks and MCP
# servers are explicitly deferred until v2; agents/skills are CC-side
# concepts that don't apply to sandboxed runs.
FORBIDDEN_IMPORT_KEYS: tuple[str, ...] = (
    "hooks", "mcpServers", "agents", "skills",
)


# Default seed profiles. Three presets matching the spec — anything else
# the user wants comes from "Save as new" in the UI. Credentials are left
# unset on purpose; the user picks an auth source before first use.
_SEED_PROFILES: tuple[dict, ...] = (
    {
        "name": "Read only",
        "description": "Inspect code without making changes.",
        "network_mode": "off",
        "allowed_tools": ["Read", "Grep", "Glob"],
        "denied_tools": ["Bash", "Edit", "Write"],
        "permission_mode": "default",
        "fs_read": [],
        "fs_write": [],
    },
    {
        "name": "Edit workspace",
        "description": "Read and edit files in the ticket workspace.",
        "network_mode": "on",
        "allowed_tools": [],
        "denied_tools": [],
        "permission_mode": "acceptEdits",
    },
    {
        "name": "Full workspace",
        "description": (
            "Unrestricted access within the ticket workspace, including shell."
        ),
        "network_mode": "on",
        "allowed_tools": [],
        "denied_tools": [],
        "permission_mode": "bypassPermissions",
    },
)


class ProfileNotFound(Exception):
    pass


class ProfileNameTaken(Exception):
    pass


def create_profile(session: Session, **fields) -> Profile:
    p = Profile(**fields)
    session.add(p)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ProfileNameTaken(fields.get("name")) from exc
    session.refresh(p)
    return p


def get_profile(session: Session, profile_id: str) -> Profile:
    p = session.get(Profile, profile_id)
    if p is None:
        raise ProfileNotFound(profile_id)
    return p


def list_profiles(session: Session) -> list[Profile]:
    return list(session.scalars(select(Profile).order_by(Profile.name)))


def update_profile(session: Session, profile_id: str, **fields) -> Profile:
    p = get_profile(session, profile_id)
    for k, v in fields.items():
        setattr(p, k, v)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ProfileNameTaken(fields.get("name")) from exc
    session.refresh(p)
    return p


def delete_profile(session: Session, profile_id: str) -> None:
    p = get_profile(session, profile_id)
    session.delete(p)
    session.commit()


def strip_forbidden_import_fields(payload: dict) -> tuple[dict, list[str]]:
    """Return a sanitized copy of an imported profile dict.

    Drops keys in ``FORBIDDEN_IMPORT_KEYS`` from both the top level and
    from ``cc_settings_passthrough`` (since those keys can also legally
    appear inside CC's settings.json). Returns the cleaned dict and a
    list of dropped field names so the caller can surface a warning.
    """
    if not isinstance(payload, dict):
        return {}, []
    cleaned = dict(payload)
    dropped: list[str] = []
    for key in FORBIDDEN_IMPORT_KEYS:
        if key in cleaned:
            cleaned.pop(key, None)
            dropped.append(key)
    nested = cleaned.get("cc_settings_passthrough")
    if isinstance(nested, dict):
        nested_clean = dict(nested)
        for key in FORBIDDEN_IMPORT_KEYS:
            if key in nested_clean:
                nested_clean.pop(key, None)
                dropped.append(f"cc_settings_passthrough.{key}")
        cleaned["cc_settings_passthrough"] = nested_clean
    return cleaned, dropped


def seed_default_profiles(engine: Engine) -> list[Profile]:
    """Insert the v1 default profiles if the table is empty.

    Idempotent: a non-empty profiles table is left untouched. Called from
    ``_init()`` during ``nightdesk-init`` and from ``run-dev`` so a fresh
    install opens to a usable profile list instead of an empty page.
    """
    with Session(engine) as session:
        existing = session.scalar(select(Profile.id).limit(1))
        if existing is not None:
            return []
        created: list[Profile] = []
        for spec in _SEED_PROFILES:
            created.append(create_profile(session, **spec))
        return created
