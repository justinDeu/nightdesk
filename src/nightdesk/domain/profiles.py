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


# Permission-mode values Nightdesk understands. Claude Code's settings.json
# may carry modes we don't model (plan / auto / dontAsk / prompt); those are
# dropped during translation so the profile keeps a default-prompt posture
# rather than silently inheriting an unsupported mode.
_CC_PERMISSION_MODES: frozenset[str] = frozenset(
    ("default", "acceptEdits", "bypassPermissions")
)

# Env keys the Authentication section owns. They must never land in the
# generic env blob via a CC import; the user re-enters credentials through
# the auth UI after importing.
_CC_AUTH_OWNED_ENV_KEYS: frozenset[str] = frozenset(
    ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
)

# Top-level CC settings keys that map onto first-class profile fields (or are
# handled explicitly). Anything outside this set is preserved verbatim in
# cc_settings_passthrough so nothing the user configured is silently lost.
_CC_CONSUMED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    ("model", "env", "permissions")
) | set(FORBIDDEN_IMPORT_KEYS)


def translate_cc_settings(settings: dict, *, name: str | None = None) -> dict:
    """Translate a Claude Code ``settings.json`` dict into profile fields.

    Claude Code uses a different shape than Nightdesk's own export, so this
    maps the CC keys onto our schema:

    - ``model``                 -> ``default_model``
    - ``env``                   -> ``env`` (auth-owned keys stripped)
    - ``permissions.allow``     -> ``allowed_tools``
    - ``permissions.deny``      -> ``denied_tools``
    - ``permissions.defaultMode`` -> ``permission_mode`` (only the modes
      Nightdesk supports; others are dropped)
    - everything else           -> ``cc_settings_passthrough`` verbatim

    Forbidden keys (``hooks``, ``mcpServers``, ``agents``, ``skills``) are
    removed up front via :func:`strip_forbidden_import_fields`, matching the
    safety applied to native imports. The result is a plain dict of profile
    fields ready for ``create_profile`` (the caller still encrypts ``env``).
    """
    if not isinstance(settings, dict):
        raise ValueError("Claude Code settings must be a JSON object")

    cleaned, _ = strip_forbidden_import_fields(settings)

    fields: dict = {}

    if isinstance(cleaned.get("model"), str) and cleaned["model"].strip():
        fields["default_model"] = cleaned["model"].strip()

    env = cleaned.get("env")
    if isinstance(env, dict):
        safe_env = {
            str(k): str(v)
            for k, v in env.items()
            if isinstance(k, str)
            and k
            and k not in _CC_AUTH_OWNED_ENV_KEYS
            and v is not None
        }
        if safe_env:
            fields["env"] = safe_env

    perms = cleaned.get("permissions")
    passthrough_perms: dict = {}
    if isinstance(perms, dict):
        allow = perms.get("allow")
        if isinstance(allow, list):
            fields["allowed_tools"] = [str(x) for x in allow if isinstance(x, str)]
        deny = perms.get("deny")
        if isinstance(deny, list):
            fields["denied_tools"] = [str(x) for x in deny if isinstance(x, str)]
        mode = perms.get("defaultMode")
        if isinstance(mode, str) and mode in _CC_PERMISSION_MODES:
            fields["permission_mode"] = mode
        # Preserve permission keys we don't map (ask, additionalDirectories,
        # disableBypassPermissionsMode, an unsupported defaultMode, ...) so
        # they survive the round-trip in passthrough.
        for k, v in perms.items():
            if k in ("allow", "deny"):
                continue
            if k == "defaultMode" and v in _CC_PERMISSION_MODES:
                continue
            passthrough_perms[k] = v

    # Anything not consumed above is parked in cc_settings_passthrough so the
    # import is lossless for keys Nightdesk doesn't (yet) promote (apiKeyHelper,
    # companyAnnouncements, statusLine, etc.). Auth-owned keys are never parked
    # here: cc_settings_passthrough is stored UNENCRYPTED, so a top-level
    # ANTHROPIC_API_KEY/AUTH_TOKEN/BASE_URL (outside the nested env block, e.g.
    # in a malformed file) must be dropped, not leaked into a plaintext column
    # readable via profile export.
    passthrough: dict = {
        k: v for k, v in cleaned.items()
        if k not in _CC_CONSUMED_TOP_LEVEL_KEYS
        and k not in _CC_AUTH_OWNED_ENV_KEYS
    }
    if passthrough_perms:
        passthrough["permissions"] = passthrough_perms
    if passthrough:
        fields["cc_settings_passthrough"] = passthrough

    if name and name.strip():
        fields["name"] = name.strip()

    return fields


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
