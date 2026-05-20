"""Profile management API.

Two parallel surfaces sit on the same domain helpers:

- ``/api/v1/profiles`` — JSON CRUD for scripts and external clients.
- ``/profiles`` — HTML editor for the browser UI (HTMX-style POSTs from
  ``<form>`` because browsers don't speak PATCH from forms).

Encrypted fields (``claude_credentials.value`` and the values inside
``env``) are sealed with ``ProfileSecretBox`` before persistence. JSON
responses never return plaintext for those fields — they expose a
``value_set`` flag plus a list of env keys so the editor can render a
rotation affordance without leaking the secret back across the wire.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_bearer, require_token_cookie_or_bearer
from nightdesk.api.schemas import ProfileCreate, ProfileOut, ProfileUpdate
from nightdesk.domain.cc_env_catalog import CC_ENV_CATALOG, categories as env_categories, lookup as env_lookup
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.profiles import (
    create_profile,
    delete_profile,
    get_profile,
    list_profiles,
    strip_forbidden_import_fields,
    translate_cc_settings,
    update_profile,
    ProfileNotFound,
    ProfileNameTaken,
)
from nightdesk.worker.sandbox import assert_no_excluded_paths


_PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions")
_NETWORK_MODES = ("off", "on")
_CREDENTIAL_SOURCES = ("inherit", "api_key", "auth_token")
_VALID_RUN_TOKEN_SCOPES = ("ticket.create",)

# Env keys with dedicated UI fieldsets in the profile editor. They share
# storage with the generic kv editor (the encrypted ``env`` blob) but the
# generic picker hides them so they don't appear in two places at once.
_MODELS_PRIMARY_KEYS: tuple[str, ...] = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)
_MODELS_OTHER_KEYS: tuple[str, ...] = (
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
_MODELS_KEYS: tuple[str, ...] = _MODELS_PRIMARY_KEYS + _MODELS_OTHER_KEYS
_BEHAVIOR_TOGGLE_KEYS: tuple[str, ...] = (
    "DISABLE_TELEMETRY",
    "DISABLE_AUTOUPDATER",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_BUG_COMMAND",
    "DISABLE_COST_WARNINGS",
    "DISABLE_ERROR_REPORTING",
    "DISABLE_PROMPT_CACHING",
)
_BEHAVIOR_NUMERIC_KEYS: tuple[str, ...] = ("MAX_THINKING_TOKENS",)

# Keys owned by the Authentication section (claude_credentials), which
# cannot be set via the env editor. The generic editor refuses to write
# them and the migration scrubbed them from existing Profile.env blobs.
_AUTH_OWNED_ENV_KEYS: frozenset[str] = frozenset(
    ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
)

_PROMOTED_KEYS: frozenset[str] = frozenset(
    _MODELS_KEYS
    + _BEHAVIOR_TOGGLE_KEYS
    + _BEHAVIOR_NUMERIC_KEYS
)

# v1 ships with one backend. The dropdown still exists so it can grow
# without a schema change, but no vaporware "coming soon" entries appear
# until the corresponding wiring lands.
_BACKEND_CHOICES = (
    ("claude_sdk", "Claude Code", True),
)


def _split_lines(value: str | None) -> list[str]:
    """Split a textarea value into a clean list of non-empty trimmed lines."""
    if not value:
        return []
    out: list[str] = []
    for raw in value.replace(",", "\n").splitlines():
        item = raw.strip()
        if item:
            out.append(item)
    return out


def _validate_paths(paths: list[str], *, field: str) -> None:
    """Reject paths overlapping protected nightdesk dirs.

    Reuses the worker-side excluded-path check so the form and the
    sandbox agree on the rule. Surfaces a 400 with the offending field
    name so the editor can highlight the right textarea.
    """
    try:
        assert_no_excluded_paths(paths)
    except ValueError as exc:
        raise HTTPException(400, f"{field}: {exc}") from exc


def _encrypt_credentials_in(
    box: Optional[ProfileSecretBox],
    payload: Any,
    existing_blob: Optional[str],
) -> Optional[str]:
    """Encrypt an inbound credentials payload for storage.

    ``payload`` is either ``None`` (no change), a dict shaped like
    ``ClaudeCredentialsIn``, or ``{}`` to clear. For env-based sources
    a missing ``value`` reuses the previous secret so PATCH callers can
    flip the source without rotating the secret.
    """
    if payload is None:
        if existing_blob is None:
            raise HTTPException(
                400,
                "claude_credentials is required; pick an authentication source",
            )
        return existing_blob
    if box is None:
        raise HTTPException(
            500,
            "profile credentials cannot be stored: bearer_token is empty so "
            "no encryption key is available",
        )
    if payload == {} or payload is False:
        raise HTTPException(
            400,
            "claude_credentials cannot be cleared; pick an authentication source",
        )
    if isinstance(payload, dict):
        data = payload
    else:
        data = payload.model_dump()
    source = data.get("source")
    if source not in _CREDENTIAL_SOURCES:
        raise HTTPException(400, f"claude_credentials.source must be one of {_CREDENTIAL_SOURCES}")
    base_url = (data.get("base_url") or "").strip() or None

    def _pack(value: Optional[str]) -> str:
        out: dict[str, Any] = {"source": source}
        if value is not None:
            out["value"] = value
        if base_url is not None:
            out["base_url"] = base_url
        return box.encrypt(out)

    if source == "inherit":
        return _pack(None)
    value = data.get("value")
    if value in (None, ""):
        # Source kept but caller didn't provide a new secret; preserve
        # the existing ciphertext's value under the new source/base_url.
        if existing_blob is None:
            raise HTTPException(
                400,
                f"claude_credentials.value is required when source='{source}' "
                "and no credential is already on file",
            )
        try:
            decoded = box.decrypt(existing_blob) or {}
        except ValueError:
            raise HTTPException(
                400,
                "previous credential is unreadable (bearer token rotated); "
                "re-enter the value",
            )
        old_value = decoded.get("value") if isinstance(decoded, dict) else None
        if not old_value:
            raise HTTPException(
                400,
                f"claude_credentials.value is required when source='{source}'",
            )
        return _pack(old_value)
    return _pack(value)


def _encrypt_env_in(
    box: Optional[ProfileSecretBox],
    payload: Optional[dict[str, str]],
) -> Optional[str]:
    if payload is None:
        return "__UNCHANGED__"  # sentinel: caller drops the field
    if not isinstance(payload, dict):
        raise HTTPException(400, "env must be a JSON object of string keys and values")
    if not payload:
        return None
    if box is None:
        raise HTTPException(
            500,
            "profile env cannot be stored: bearer_token is empty so no "
            "encryption key is available",
        )
    cleaned: dict[str, str] = {}
    for k, v in payload.items():
        if not isinstance(k, str) or not k:
            raise HTTPException(400, "env keys must be non-empty strings")
        if not isinstance(v, str):
            raise HTTPException(400, f"env[{k}] must be a string")
        if k in _AUTH_OWNED_ENV_KEYS:
            raise HTTPException(
                400,
                f"env[{k}] is reserved for the authentication section; "
                "set it via claude_credentials instead",
            )
        cleaned[k] = v
    return box.encrypt(cleaned)


def _profile_out(profile, box: Optional[ProfileSecretBox]) -> dict:
    """Serialize a Profile for JSON responses, redacting secrets."""
    creds_out = None
    if profile.claude_credentials:
        try:
            decoded = (box.decrypt(profile.claude_credentials)
                       if box is not None else None)
        except ValueError:
            decoded = None
        source = (decoded or {}).get("source") if isinstance(decoded, dict) else None
        if source in _CREDENTIAL_SOURCES:
            has_value = source != "inherit" and bool((decoded or {}).get("value"))
            base_url = (decoded or {}).get("base_url") or None
            creds_out = {
                "source": source,
                "value_set": bool(has_value),
                "base_url": base_url,
            }
    env_keys: list[str] = []
    if profile.env:
        try:
            decoded_env = box.decrypt(profile.env) if box is not None else None
        except ValueError:
            decoded_env = None
        if isinstance(decoded_env, dict):
            env_keys = sorted(decoded_env.keys())
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description or "",
        "fs_read": profile.fs_read or [],
        "fs_write": profile.fs_write or [],
        "allowed_tools": profile.allowed_tools or [],
        "denied_tools": profile.denied_tools or [],
        "network_mode": profile.network_mode,
        "network_allowlist": profile.network_allowlist or [],
        "secret_keys": profile.secret_keys or [],
        "default_model": profile.default_model,
        "backend": profile.backend,
        "claude_credentials": creds_out,
        "claude_binary_path": profile.claude_binary_path,
        "env_keys": env_keys,
        "system_prompt": profile.system_prompt,
        "permission_mode": profile.permission_mode,
        "cc_settings_passthrough": profile.cc_settings_passthrough or {},
        "run_token_scopes": profile.run_token_scopes or [],
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


def _profile_export(profile, box: Optional[ProfileSecretBox]) -> dict:
    """Build the JSON export of a profile.

    Secrets are intentionally redacted: ``claude_credentials.value`` is
    dropped (only the source is preserved), and ``env`` becomes a list of
    keys without values. Importing the same JSON elsewhere requires the
    user to re-enter the actual secret values.
    """
    out = _profile_out(profile, box)
    out.pop("id", None)
    out.pop("created_at", None)
    out.pop("updated_at", None)
    if out.get("claude_credentials"):
        out["claude_credentials"] = {
            "source": out["claude_credentials"]["source"],
            "value": None,
        }
    return out


# ---------------------------------------------------------------------------
# JSON API (/api/v1/profiles)
# ---------------------------------------------------------------------------


def _build_json_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/profiles",
        tags=["profiles"],
        dependencies=[Depends(require_bearer(bearer_token))],
    )
    box = ProfileSecretBox(bearer_token) if bearer_token else None

    @router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
    async def create(payload: ProfileCreate, session: Session = Depends(get_session)):
        fields = payload.model_dump()
        creds_in = fields.pop("claude_credentials", None)
        env_in = fields.pop("env", None)
        fields["claude_credentials"] = _encrypt_credentials_in(box, creds_in, None)
        env_token = _encrypt_env_in(box, env_in)
        fields["env"] = None if env_token == "__UNCHANGED__" else env_token
        _validate_paths(fields.get("fs_read") or [], field="fs_read")
        _validate_paths(fields.get("fs_write") or [], field="fs_write")
        if fields.get("network_mode") and fields["network_mode"] not in _NETWORK_MODES:
            raise HTTPException(400, f"network_mode must be one of {_NETWORK_MODES}")
        for scope in fields.get("run_token_scopes") or []:
            if scope not in _VALID_RUN_TOKEN_SCOPES:
                raise HTTPException(400, f"unknown run_token_scope {scope!r}")
        try:
            profile = create_profile(session, **fields)
        except ProfileNameTaken:
            raise HTTPException(409, "name taken")
        return _profile_out(profile, box)

    @router.get("", response_model=list[ProfileOut])
    async def lst(session: Session = Depends(get_session)):
        return [_profile_out(p, box) for p in list_profiles(session)]

    @router.get("/{pid}", response_model=ProfileOut)
    async def show(pid: str, session: Session = Depends(get_session)):
        try:
            return _profile_out(get_profile(session, pid), box)
        except ProfileNotFound:
            raise HTTPException(404, "not found")

    @router.patch("/{pid}", response_model=ProfileOut)
    async def update(pid: str, payload: ProfileUpdate, session: Session = Depends(get_session)):
        try:
            existing = get_profile(session, pid)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        raw = payload.model_dump(exclude_unset=True)
        fields: dict[str, Any] = {k: v for k, v in raw.items()
                                  if k not in ("claude_credentials", "env")}
        if "claude_credentials" in raw:
            fields["claude_credentials"] = _encrypt_credentials_in(
                box, raw["claude_credentials"], existing.claude_credentials,
            )
        if "env" in raw:
            env_token = _encrypt_env_in(box, raw["env"])
            if env_token != "__UNCHANGED__":
                fields["env"] = env_token
        if "fs_read" in fields:
            _validate_paths(fields["fs_read"] or [], field="fs_read")
        if "fs_write" in fields:
            _validate_paths(fields["fs_write"] or [], field="fs_write")
        if fields.get("network_mode") and fields["network_mode"] not in _NETWORK_MODES:
            raise HTTPException(400, f"network_mode must be one of {_NETWORK_MODES}")
        for scope in fields.get("run_token_scopes") or []:
            if scope not in _VALID_RUN_TOKEN_SCOPES:
                raise HTTPException(400, f"unknown run_token_scope {scope!r}")
        try:
            profile = update_profile(session, pid, **fields)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        except ProfileNameTaken:
            raise HTTPException(409, "name taken")
        return _profile_out(profile, box)

    @router.delete("/{pid}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete(pid: str, session: Session = Depends(get_session)):
        try:
            delete_profile(session, pid)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        return None

    return router


# ---------------------------------------------------------------------------
# HTML editor (/profiles, /profiles/new, /profiles/{id})
# ---------------------------------------------------------------------------


def _decrypt_creds_for_form(profile, box: Optional[ProfileSecretBox]) -> dict:
    """Return the credential view-model the editor renders against.

    Never returns the secret value. The template uses ``source`` to
    select the right radio, ``value_set`` to decide between an input
    and a "rotate" affordance, and ``base_url`` to prefill the optional
    custom-endpoint field.
    """
    if not profile.claude_credentials or box is None:
        return {"source": None, "value_set": False, "base_url": ""}
    try:
        decoded = box.decrypt(profile.claude_credentials) or {}
    except ValueError:
        return {"source": None, "value_set": False, "base_url": "", "unreadable": True}
    source = decoded.get("source") if isinstance(decoded, dict) else None
    if source not in _CREDENTIAL_SOURCES:
        return {"source": None, "value_set": False, "base_url": ""}
    has_value = source != "inherit" and bool(decoded.get("value"))
    base_url = (decoded.get("base_url") or "") if isinstance(decoded, dict) else ""
    return {"source": source, "value_set": has_value, "base_url": base_url}


def _env_keys_for_form(profile, box: Optional[ProfileSecretBox]) -> list[str]:
    if not profile.env or box is None:
        return []
    try:
        decoded = box.decrypt(profile.env) or {}
    except ValueError:
        return []
    if not isinstance(decoded, dict):
        return []
    return sorted(decoded.keys())


def _decrypt_env_blob(profile, box: Optional[ProfileSecretBox]) -> dict[str, str]:
    """Return the decrypted env dict (or {} on absence / decryption failure)."""
    if not profile or not profile.env or box is None:
        return {}
    try:
        decoded = box.decrypt(profile.env) or {}
    except ValueError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    # Coerce values to strings defensively; the encrypt path enforces this
    # already but a corrupted blob shouldn't blow up the form render.
    return {str(k): str(v) for k, v in decoded.items()}


def _filtered_env_catalog_categories() -> list[tuple[str, list]]:
    """Like ``env_categories()`` but with promoted keys removed.

    The dropdown should never offer a variable that already has a
    dedicated section, otherwise the user has two doors to the same key
    and the round-trip behavior gets confusing.
    """
    out: list[tuple[str, list]] = []
    for category, vars_ in env_categories():
        kept = [v for v in vars_ if v.name not in _PROMOTED_KEYS]
        if kept:
            out.append((category, kept))
    return out


def _promoted_sections_context(env_blob: dict[str, str]) -> dict:
    """Build the view-model for the Models / Routing / Behavior fieldsets.

    Each section reads its current values out of the decrypted env blob
    so the form can prefill. Secret fields (the routing auth token) are
    never echoed back; the template renders an empty input and the save
    path treats blank as "keep what's on file".
    """
    def _model_row(name: str, label: str) -> dict:
        return {
            "name": name,
            "label": label,
            "value": env_blob.get(name, ""),
            "example": env_lookup(name).example if env_lookup(name) else "",
        }

    models_primary = [
        _model_row("ANTHROPIC_DEFAULT_OPUS_MODEL", "Opus alias"),
        _model_row("ANTHROPIC_DEFAULT_SONNET_MODEL", "Sonnet alias"),
        _model_row("ANTHROPIC_DEFAULT_HAIKU_MODEL", "Haiku alias"),
    ]
    models_other = [
        _model_row("ANTHROPIC_MODEL", "Primary model"),
        _model_row("CLAUDE_CODE_SUBAGENT_MODEL", "Subagent model"),
    ]
    behavior_toggles = [
        {
            "name": name,
            "checked": env_blob.get(name) == "1",
            "description": (
                env_lookup(name).description if env_lookup(name) else ""
            ),
        }
        for name in _BEHAVIOR_TOGGLE_KEYS
    ]
    behavior_numeric = {
        "max_thinking_tokens": env_blob.get("MAX_THINKING_TOKENS", ""),
    }
    return {
        "promoted_models_primary": models_primary,
        "promoted_models_other": models_other,
        "promoted_behavior_toggles": behavior_toggles,
        "promoted_behavior_numeric": behavior_numeric,
    }


def _daemon_claude_binary(session: Session) -> str:
    """The claude binary the worker would use when a profile leaves it blank.

    Mirrors worker.run_one._resolve_default_claude_binary: the config row's
    override if set, else whatever `claude` resolves to on PATH. Drives the
    profile editor's placeholder so the ghost text shows the *actual* default
    rather than a generic hint.
    """
    from nightdesk.db.models import ConfigRow

    cfg_row = session.get(ConfigRow, 1)
    configured = getattr(cfg_row, "claude_binary_path", None)
    return configured or shutil.which("claude") or "claude"


def _shared_form_context() -> dict:
    """Constants every editor context needs: backends, env catalog, modes."""
    return {
        "permission_modes": _PERMISSION_MODES,
        "credential_sources": _CREDENTIAL_SOURCES,
        "network_modes": _NETWORK_MODES,
        "backend_choices": _BACKEND_CHOICES,
        "env_catalog": CC_ENV_CATALOG,
        "env_catalog_categories": _filtered_env_catalog_categories(),
        "promoted_keys": _PROMOTED_KEYS,
    }


def _form_context(profile, box: Optional[ProfileSecretBox]) -> dict:
    """Build the editor template context for an existing profile."""
    env_blob = _decrypt_env_blob(profile, box)
    # Generic kv editor only renders keys that aren't promoted into a
    # dedicated section. The promoted ones are still in the blob; they
    # just have nicer UI elsewhere on the form.
    env_keys = sorted(k for k in env_blob.keys() if k not in _PROMOTED_KEYS)
    return {
        "profile": profile,
        "credentials": _decrypt_creds_for_form(profile, box),
        "env_keys": env_keys,
        "env_known_keys": [k for k in env_keys if env_lookup(k) is not None],
        "env_custom_keys": [k for k in env_keys if env_lookup(k) is None],
        "fs_read_text": "\n".join(profile.fs_read or []),
        "fs_write_text": "\n".join(profile.fs_write or []),
        "allowed_tools_text": ", ".join(profile.allowed_tools or []),
        "denied_tools_text": ", ".join(profile.denied_tools or []),
        "secret_keys_text": "\n".join(profile.secret_keys or []),
        "cc_settings_json": json.dumps(
            profile.cc_settings_passthrough or {}, indent=2, sort_keys=True,
        ),
        **_shared_form_context(),
        **_promoted_sections_context(env_blob),
    }


def _empty_form_context() -> dict:
    """Editor context for the 'New profile' page (no Profile row yet)."""
    return {
        "profile": None,
        "credentials": {"source": "inherit", "value_set": False, "base_url": ""},
        "env_keys": [],
        "env_known_keys": [],
        "env_custom_keys": [],
        "fs_read_text": "",
        "fs_write_text": "",
        "allowed_tools_text": "",
        "denied_tools_text": "",
        "secret_keys_text": "",
        "cc_settings_json": "{}",
        **_shared_form_context(),
        **_promoted_sections_context({}),
    }


def _view_context(profile, box: Optional[ProfileSecretBox]) -> dict:
    """Read-only view-model for the right pane.

    Credential values never appear. Non-secret env values are shown verbatim
    (model overrides, base URLs the agent will see anyway, behavior toggles);
    catalog entries flagged ``secret=True`` collapse to "value set" so a key
    leak doesn't expose the secret.
    """
    env_blob = _decrypt_env_blob(profile, box)
    model_overrides: list[dict] = []
    for key in _MODELS_KEYS:
        if key in env_blob:
            model_overrides.append({"name": key, "value": env_blob[key]})
    env_entries: list[dict] = []
    for key in sorted(env_blob):
        if key in _MODELS_KEYS:
            continue  # already surfaced under model_overrides
        known = env_lookup(key)
        is_secret = bool(known and known.secret)
        env_entries.append({
            "name": key,
            "value": None if is_secret else env_blob[key],
            "is_secret": is_secret,
            "description": known.description if known else "",
        })
    return {
        "profile": profile,
        "credentials": _decrypt_creds_for_form(profile, box),
        "env_keys": _env_keys_for_form(profile, box),
        "env_entries": env_entries,
        "model_overrides": model_overrides,
        "fs_read_lines": list(profile.fs_read or []),
        "fs_write_lines": list(profile.fs_write or []),
        "allowed_tools_list": list(profile.allowed_tools or []),
        "denied_tools_list": list(profile.denied_tools or []),
        "backend_label": next(
            (label for code, label, _ in _BACKEND_CHOICES if code == profile.backend),
            profile.backend,
        ),
        "env_catalog_lookup": env_lookup,
    }


def _parse_env_form(
    form,
    *,
    existing_blob: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Read env-kv pairs from the form. Returns None if no kv editor was
    submitted, so the route knows not to touch the existing env.

    The form has two ways to set env vars:

    1. The generic kv picker (``env_name`` / ``env_value`` lists). The
       submission marker is ``env_field_submitted``.
    2. The dedicated Models / Routing / Behavior fieldsets, marked
       ``promoted_env_submitted``. Their fields use namespaced input
       names (``promoted_model_<KEY>``, ``promoted_routing_base_url``,
       ``promoted_routing_auth_token``, ``promoted_behavior_<KEY>``,
       ``promoted_max_thinking_tokens``).

    Either marker triggers a write. Promoted-section values overwrite
    same-named keys coming out of the generic editor, since the user
    just submitted the form with the dedicated fields visible. For the
    secret routing token an empty input means "keep what's on file",
    preserving the existing value from ``existing_blob``.
    """
    has_generic = "env_field_submitted" in form
    has_promoted = "promoted_env_submitted" in form
    if not has_generic and not has_promoted:
        return None

    out: dict[str, str] = {}

    if has_generic:
        names = form.getlist("env_name")
        values = form.getlist("env_value")
        rotates = set(form.getlist("env_rotate"))
        for idx, name in enumerate(names):
            key = (name or "").strip()
            if not key:
                continue
            # Reject anything that should have come in through a promoted
            # section so a stray duplicate row can't shadow it.
            if key in _PROMOTED_KEYS:
                continue
            # The Authentication section owns these — silently drop attempts
            # to set them via the generic editor.
            if key in _AUTH_OWNED_ENV_KEYS:
                continue
            val = values[idx] if idx < len(values) else ""
            if key in rotates and not val:
                # Existing row left blank: "leave blank to keep" (matches the
                # form helper text). Preserve the value already on file so a
                # plain save — or a save-as-new / copy — doesn't silently wipe
                # env vars that were never echoed back into the form. To clear
                # a value the user removes the whole row (its env_name stops
                # being submitted), not blanks the input.
                if existing_blob and key in existing_blob:
                    out[key] = existing_blob[key]
                continue
            out[key] = val or ""

    if has_promoted:
        # Models: free-text inputs. Empty means "don't set".
        for key in _MODELS_KEYS:
            val = (form.get(f"promoted_model_{key}") or "").strip()
            if val:
                out[key] = val
            else:
                out.pop(key, None)

        # Behavior toggles: checkboxes write "1" when checked.
        for key in _BEHAVIOR_TOGGLE_KEYS:
            field = f"promoted_behavior_{key}"
            if form.get(field):
                out[key] = "1"
            else:
                out.pop(key, None)

        # Numeric: empty stays unset.
        mtt = (form.get("promoted_max_thinking_tokens") or "").strip()
        if mtt:
            out["MAX_THINKING_TOKENS"] = mtt
        else:
            out.pop("MAX_THINKING_TOKENS", None)

        # If only the promoted form was submitted, preserve any
        # non-promoted keys already on file so a save of the Models
        # section doesn't silently wipe the user's custom env vars.
        if not has_generic and existing_blob:
            for k, v in existing_blob.items():
                if k not in _PROMOTED_KEYS and k not in out:
                    out[k] = v

    return out


def _parse_credentials_form(form, existing_blob: Optional[str]) -> Any:
    """Translate the credential radio + value + base_url fields to API payload."""
    if "credentials_field_submitted" not in form:
        return None
    source = (form.get("credentials_source") or "").strip()
    if not source:
        raise HTTPException(400, "credentials_source is required")
    if source not in _CREDENTIAL_SOURCES:
        raise HTTPException(400, f"credentials_source must be one of {_CREDENTIAL_SOURCES}")
    base_url = (form.get("credentials_base_url") or "").strip() or None
    payload: dict[str, Any] = {"source": source}
    if base_url is not None:
        payload["base_url"] = base_url
    if source == "inherit":
        return payload
    value = (form.get("credentials_value") or "").strip()
    if value:
        payload["value"] = value
    # No value submitted: _encrypt_credentials_in reuses the existing ciphertext.
    return payload


def _form_fields_to_update(
    form,
    *,
    box: Optional[ProfileSecretBox],
    existing,
) -> dict[str, Any]:
    """Map form fields to Profile column updates.

    Validates paths against the protected-nightdesk-dir rule; raises 400
    so the surrounding route can re-render the form with the error.
    """
    fields: dict[str, Any] = {}
    if "name" in form:
        name = (form.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name is required")
        fields["name"] = name
    if "description" in form:
        fields["description"] = (form.get("description") or "").strip()
    if "backend" in form:
        backend = (form.get("backend") or "").strip()
        # Only enabled backends from the picker are accepted. Submitting a
        # disabled value (e.g. via curl) is rejected so we don't pretend to
        # support a backend that isn't wired up.
        enabled = {code for code, _, on in _BACKEND_CHOICES if on}
        if backend not in enabled:
            raise HTTPException(400, f"backend must be one of {sorted(enabled)}")
        fields["backend"] = backend
    if "default_model" in form:
        fields["default_model"] = (form.get("default_model") or "").strip() or None
    if "system_prompt" in form:
        fields["system_prompt"] = (form.get("system_prompt") or "").strip() or None
    if "permission_mode" in form:
        pm = (form.get("permission_mode") or "").strip()
        if pm and pm not in _PERMISSION_MODES:
            raise HTTPException(400, f"permission_mode must be one of {_PERMISSION_MODES}")
        fields["permission_mode"] = pm or None
    if "network_mode" in form:
        nm = (form.get("network_mode") or "").strip()
        if nm not in _NETWORK_MODES:
            raise HTTPException(400, f"network_mode must be one of {_NETWORK_MODES}")
        fields["network_mode"] = nm
    if "claude_binary_path" in form:
        cb = (form.get("claude_binary_path") or "").strip()
        fields["claude_binary_path"] = cb or None
    if "allowed_tools" in form:
        fields["allowed_tools"] = _split_lines(form.get("allowed_tools"))
    if "denied_tools" in form:
        fields["denied_tools"] = _split_lines(form.get("denied_tools"))
    if "secret_keys" in form:
        fields["secret_keys"] = _split_lines(form.get("secret_keys"))
    if "fs_read" in form:
        paths = _split_lines(form.get("fs_read"))
        _validate_paths(paths, field="fs_read")
        fields["fs_read"] = paths
    if "fs_write" in form:
        paths = _split_lines(form.get("fs_write"))
        _validate_paths(paths, field="fs_write")
        fields["fs_write"] = paths
    if "run_token_scope_ticket_create" in form:
        scopes = list(existing.run_token_scopes or []) if existing else []
        granted = form.get("run_token_scope_ticket_create") == "on"
        scopes = [s for s in scopes if s != "ticket.create"]
        if granted:
            scopes.append("ticket.create")
        fields["run_token_scopes"] = scopes
    creds_in = _parse_credentials_form(form, existing.claude_credentials if existing else None)
    if creds_in is not None:
        fields["claude_credentials"] = _encrypt_credentials_in(
            box, creds_in, existing.claude_credentials if existing else None,
        )
    existing_env = _decrypt_env_blob(existing, box) if existing else {}
    env_in = _parse_env_form(form, existing_blob=existing_env)
    if env_in is not None:
        token = _encrypt_env_in(box, env_in)
        if token != "__UNCHANGED__":
            fields["env"] = token
    return fields


def _build_html_router(
    get_session,
    bearer_token: str,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter(tags=["profiles-ui"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))
    box = ProfileSecretBox(bearer_token) if bearer_token else None

    def _render_page(
        request,
        session,
        *,
        pane_mode: str,
        selected: Any = None,
        flash: Optional[str] = None,
        partial: bool = False,
    ):
        """Render the two-pane profiles page (or just the right pane).

        ``pane_mode`` is one of ``'empty'``, ``'view'``, ``'edit'``, ``'new'``.
        ``partial=True`` returns only the right pane fragment so HTMX clicks
        in the sidebar can swap in place without re-rendering the sidebar
        itself. The sidebar is always the list of every profile so users can
        flip between profiles with one click.
        """
        profiles = list_profiles(session)
        ctx: dict = {
            "title": "Profiles",
            "active_page": "profiles",
            "profiles": profiles,
            "selected_id": selected.id if selected is not None and hasattr(selected, "id") else None,
            "pane_mode": pane_mode,
            "flash": flash,
            "daemon_claude_binary": _daemon_claude_binary(session),
        }
        if pane_mode == "view" and selected is not None:
            ctx.update(_view_context(selected, box))
        elif pane_mode == "edit" and selected is not None:
            ctx.update(_form_context(selected, box))
            ctx["form_action"] = f"/profiles/{selected.id}"
        elif pane_mode == "new":
            ctx.update(_empty_form_context())
            ctx["form_action"] = "/profiles"
        template = "partials/profile_pane.html" if partial else "profiles.html"
        return templates.TemplateResponse(request, template, ctx)

    @router.get("/profiles", response_class=HTMLResponse, dependencies=[auth])
    async def list_page(
        request: Request,
        session: Session = Depends(get_session),
        flash: Optional[str] = None,
    ):
        # The list view picks the first profile as the default selection so
        # the right pane isn't a blank slate on initial load. If the user
        # truly has no profiles yet (fresh install, seeds disabled), fall
        # back to the empty state with a "New profile" CTA.
        profiles = list_profiles(session)
        if profiles:
            return _render_page(request, session, pane_mode="view",
                                selected=profiles[0], flash=flash)
        return _render_page(request, session, pane_mode="empty", flash=flash)

    @router.get("/profiles/new", response_class=HTMLResponse, dependencies=[auth])
    async def new_form(request: Request, session: Session = Depends(get_session)):
        return _render_page(request, session, pane_mode="new")

    @router.get("/profiles/{pid}", response_class=HTMLResponse, dependencies=[auth])
    async def view_profile(pid: str, request: Request,
                           session: Session = Depends(get_session)):
        try:
            profile = get_profile(session, pid)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        partial = request.headers.get("HX-Request") == "true"
        return _render_page(request, session, pane_mode="view",
                            selected=profile, partial=partial)

    @router.get("/profiles/{pid}/edit", response_class=HTMLResponse, dependencies=[auth])
    async def edit_profile(pid: str, request: Request,
                           session: Session = Depends(get_session)):
        try:
            profile = get_profile(session, pid)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        partial = request.headers.get("HX-Request") == "true"
        return _render_page(request, session, pane_mode="edit",
                            selected=profile, partial=partial)

    @router.post("/profiles/import", dependencies=[auth])
    async def import_profile(
        request: Request,
        file: UploadFile = File(...),
        session: Session = Depends(get_session),
    ):
        raw = await file.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, f"upload is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            raise HTTPException(400, "import payload must be a JSON object")
        cleaned, dropped = strip_forbidden_import_fields(payload)
        creds_in = cleaned.pop("claude_credentials", None)
        env_in = cleaned.pop("env_keys", None)
        if "env" in cleaned and isinstance(cleaned["env"], dict):
            env_in = cleaned.pop("env")
        else:
            cleaned.pop("env", None)
        for k in ("id", "created_at", "updated_at"):
            cleaned.pop(k, None)
        allowed_keys = {
            "name", "description", "fs_read", "fs_write", "allowed_tools",
            "denied_tools", "network_mode", "network_allowlist", "secret_keys",
            "default_model", "backend", "claude_binary_path",
            "system_prompt", "permission_mode", "cc_settings_passthrough",
            "run_token_scopes",
        }
        fields = {k: v for k, v in cleaned.items() if k in allowed_keys}
        if not fields.get("name"):
            raise HTTPException(400, "import payload is missing 'name'")
        _validate_paths(fields.get("fs_read") or [], field="fs_read")
        _validate_paths(fields.get("fs_write") or [], field="fs_write")
        if isinstance(creds_in, dict) and creds_in.get("source") in _CREDENTIAL_SOURCES:
            if creds_in["source"] == "inherit":
                fields["claude_credentials"] = (
                    box.encrypt({"source": "inherit"}) if box is not None else None
                )
        if isinstance(env_in, dict) and env_in and box is not None:
            fields["env"] = box.encrypt({
                k: v for k, v in env_in.items()
                if isinstance(k, str) and isinstance(v, str)
            })
        try:
            profile = create_profile(session, **fields)
        except ProfileNameTaken:
            raise HTTPException(409, "name taken")
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse(
                {"id": profile.id, "dropped_fields": dropped},
                status_code=201,
            )
        flash = "Imported."
        if dropped:
            flash += " Dropped forbidden fields: " + ", ".join(dropped)
        return RedirectResponse(
            url=f"/profiles/{profile.id}?flash=" + flash.replace(" ", "%20"),
            status_code=303,
        )

    @router.post("/profiles/import-cc", dependencies=[auth])
    async def import_cc_settings(
        request: Request,
        file: UploadFile = File(...),
        session: Session = Depends(get_session),
    ):
        """Import a Claude Code ``settings.json`` and translate it to a profile.

        CC uses a different syntax than Nightdesk's own export (``model``,
        ``env``, ``permissions.allow``/``deny``/``defaultMode``, plus keys we
        don't model). ``translate_cc_settings`` maps those onto our schema and
        parks the remainder in ``cc_settings_passthrough``. Forbidden keys
        (hooks/mcpServers/agents/skills) are stripped with the same safety as
        the native importer. The uploaded filename (or an explicit ``name``
        form field) seeds the profile name.
        """
        raw = await file.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, f"upload is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            raise HTTPException(400, "Claude Code settings must be a JSON object")

        # Re-strip and report dropped forbidden keys for the flash message.
        _, dropped = strip_forbidden_import_fields(payload)

        form = await request.form()
        explicit_name = (form.get("name") or "").strip() or None
        fallback_name = explicit_name
        if not fallback_name and file.filename:
            stem = Path(file.filename).stem or file.filename
            fallback_name = f"CC import ({stem})"
        if not fallback_name:
            fallback_name = "Imported from Claude Code"

        try:
            fields = translate_cc_settings(payload, name=fallback_name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        if not fields.get("name"):
            fields["name"] = fallback_name
        _validate_paths(fields.get("fs_read") or [], field="fs_read")
        _validate_paths(fields.get("fs_write") or [], field="fs_write")
        if fields.get("permission_mode") and fields["permission_mode"] not in _PERMISSION_MODES:
            raise HTTPException(400, f"permission_mode must be one of {_PERMISSION_MODES}")

        env_in = fields.pop("env", None)
        if isinstance(env_in, dict) and env_in and box is not None:
            fields["env"] = box.encrypt({
                k: v for k, v in env_in.items()
                if isinstance(k, str) and isinstance(v, str)
            })

        try:
            profile = create_profile(session, **fields)
        except ProfileNameTaken:
            raise HTTPException(409, "name taken")

        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse(
                {"id": profile.id, "dropped_fields": dropped},
                status_code=201,
            )
        flash = "Imported from Claude Code settings.json."
        if dropped:
            flash += " Dropped forbidden fields: " + ", ".join(dropped)
        return RedirectResponse(
            url=f"/profiles/{profile.id}?flash=" + flash.replace(" ", "%20"),
            status_code=303,
        )

    @router.post("/profiles", dependencies=[auth])
    async def create_from_form(request: Request, session: Session = Depends(get_session)):
        form = await request.form()
        try:
            fields = _form_fields_to_update(form, box=box, existing=None)
        except HTTPException:
            raise
        if "name" not in fields:
            raise HTTPException(400, "name is required")
        try:
            profile = create_profile(session, **fields)
        except ProfileNameTaken:
            raise HTTPException(409, "name taken")
        # Land in view mode so the user immediately sees what they saved
        # instead of dropping straight back into the editor.
        return RedirectResponse(url=f"/profiles/{profile.id}", status_code=303)

    @router.post("/profiles/{pid}", dependencies=[auth])
    async def update_from_form(
        pid: str,
        request: Request,
        session: Session = Depends(get_session),
    ):
        try:
            existing = get_profile(session, pid)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        form = await request.form()
        fields = _form_fields_to_update(form, box=box, existing=existing)
        save_as_new = form.get("save_as_new") == "1"
        if save_as_new:
            new_name = (form.get("save_as_new_name") or "").strip()
            if not new_name:
                raise HTTPException(400, "save_as_new_name is required when saving as new")
            fields["name"] = new_name
            try:
                profile = create_profile(session, **fields)
            except ProfileNameTaken:
                raise HTTPException(409, "name taken")
            return RedirectResponse(url=f"/profiles/{profile.id}", status_code=303)
        try:
            update_profile(session, pid, **fields)
        except ProfileNameTaken:
            raise HTTPException(409, "name taken")
        return RedirectResponse(url=f"/profiles/{pid}", status_code=303)

    @router.post("/profiles/{pid}/delete", dependencies=[auth])
    async def delete_from_form(pid: str, session: Session = Depends(get_session)):
        try:
            delete_profile(session, pid)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        return RedirectResponse(url="/profiles", status_code=303)

    @router.post("/profiles/{pid}/copy", dependencies=[auth])
    async def copy_profile(pid: str, session: Session = Depends(get_session)):
        """Clone an existing profile and land in edit mode on the copy.

        Name collision: tries ``"<name> (copy)"``, then ``"<name> (copy 2)"``,
        etc. so repeated clicks don't error out.
        """
        try:
            src = get_profile(session, pid)
        except ProfileNotFound:
            raise HTTPException(404, "not found")

        existing_names = {p.name for p in list_profiles(session)}

        def _next_copy_name(base: str) -> str:
            candidate = f"{base} (copy)"
            if candidate not in existing_names:
                return candidate
            for i in range(2, 100):
                candidate = f"{base} (copy {i})"
                if candidate not in existing_names:
                    return candidate
            raise HTTPException(409, "too many copies of this profile")

        new_name = _next_copy_name(src.name)
        fields = {
            "name": new_name,
            "description": src.description,
            "fs_read": list(src.fs_read or []),
            "fs_write": list(src.fs_write or []),
            "allowed_tools": list(src.allowed_tools or []),
            "denied_tools": list(src.denied_tools or []),
            "network_mode": src.network_mode,
            "network_allowlist": list(src.network_allowlist or []),
            "secret_keys": list(src.secret_keys or []),
            "default_model": src.default_model,
            "backend": src.backend,
            "claude_credentials": src.claude_credentials,
            "claude_binary_path": src.claude_binary_path,
            "env": src.env,
            "system_prompt": src.system_prompt,
            "permission_mode": src.permission_mode,
            "cc_settings_passthrough": dict(src.cc_settings_passthrough or {}),
            "run_token_scopes": list(src.run_token_scopes or []),
        }
        try:
            new_profile = create_profile(session, **fields)
        except ProfileNameTaken:
            raise HTTPException(409, f"name {new_name!r} taken")
        return RedirectResponse(
            url=f"/profiles/{new_profile.id}/edit", status_code=303,
        )

    @router.get("/profiles/{pid}/export", dependencies=[auth])
    async def export_profile(pid: str, session: Session = Depends(get_session)):
        try:
            profile = get_profile(session, pid)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        payload = _profile_export(profile, box)
        body = json.dumps(payload, indent=2, sort_keys=True, default=str)
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="nightdesk-profile-{profile.name}.json"'
                ),
            },
        )

    return router


def build_router(get_session, bearer_token: str) -> APIRouter:
    """JSON-only router. Kept for ``api/app.py`` backwards compatibility.

    The HTML editor lives in ``build_html_router`` and is wired up
    separately from ``create_app`` so it can take the ``templates``
    object.
    """
    return _build_json_router(get_session, bearer_token)


def build_html_router(
    get_session,
    bearer_token: str,
    templates: Jinja2Templates,
) -> APIRouter:
    """HTML editor router (cookie or bearer auth)."""
    return _build_html_router(get_session, bearer_token, templates)
