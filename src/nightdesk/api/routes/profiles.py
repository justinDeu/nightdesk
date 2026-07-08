"""Profile management API (JSON, ``/api/v1/profiles``).

Encrypted fields (``claude_credentials.value`` and the values inside
``env``) are sealed with ``ProfileSecretBox`` before persistence. JSON
responses never return plaintext for those fields — they expose a
``value_set`` flag plus a list of env keys so a client can render a
rotation affordance without leaking the secret back across the wire.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.api.schemas import (
    ProfileCreate, ProfileImport, ProfileImportFromCC, ProfileImportResult, ProfileOut,
    ProfileUpdate,
)
from nightdesk.domain.backend_capabilities import DEFAULT_BACKEND, capability_or_default
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.providers import (
    EndpointNotFound,
    endpoint_compatible,
    get_endpoint,
)
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

# Keys owned by the Authentication section (claude_credentials), which
# cannot be set via the env editor. The generic editor refuses to write
# them and the migration scrubbed them from existing Profile.env blobs.
_AUTH_OWNED_ENV_KEYS: frozenset[str] = frozenset(
    ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
)


def _next_copy_name(existing_names: set[str], base: str) -> str:
    """First unused ``"<base> (copy)"`` / ``"<base> (copy N)"`` name."""
    candidate = f"{base} (copy)"
    if candidate not in existing_names:
        return candidate
    for i in range(2, 100):
        candidate = f"{base} (copy {i})"
        if candidate not in existing_names:
            return candidate
    raise HTTPException(409, "too many copies of this profile")


def _copy_fields(src) -> dict[str, Any]:
    """Field set for cloning a profile."""
    return {
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
        "endpoint_id": src.endpoint_id,
        "backend_config": dict(src.backend_config or {}),
        "claude_credentials": src.claude_credentials,
        "claude_binary_path": src.claude_binary_path,
        "env": src.env,
        "system_prompt": src.system_prompt,
        "permission_mode": src.permission_mode,
        "cc_settings_passthrough": dict(src.cc_settings_passthrough or {}),
        "run_token_scopes": list(src.run_token_scopes or []),
    }


def _validate_paths(paths: list[str], *, field: str) -> None:
    """Reject paths overlapping protected nightdesk dirs.

    Reuses the worker-side excluded-path check so the API and the sandbox
    agree on the rule. Surfaces a 400 naming the offending field.
    """
    try:
        assert_no_excluded_paths(paths)
    except ValueError as exc:
        raise HTTPException(400, f"{field}: {exc}") from exc


def _encrypt_credentials_in(
    box: Optional[ProfileSecretBox],
    payload: Any,
    existing_blob: Optional[str],
    *,
    endpoint_id: Optional[str] = None,
) -> Optional[str]:
    """Encrypt an inbound credentials payload for storage.

    ``payload`` is either ``None`` (no change), a dict shaped like
    ``ClaudeCredentialsIn``, or ``{}`` to clear. For env-based sources
    a missing ``value`` reuses the previous secret so PATCH callers can
    flip the source without rotating the secret.

    ``endpoint_id``, when set, means a ``ProviderEndpoint`` supplies
    authentication, so ``payload is None`` with no ``existing_blob`` is
    not an error — legacy credentials are only mandatory as a fallback
    when there's no endpoint to rely on.
    """
    if payload is None:
        if existing_blob is None:
            if endpoint_id:
                return None
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


def _compat_message(capability, ep) -> str:
    """Human-readable reason ``ep`` fails the compatibility gate for ``capability``.

    See ``docs/design/providers-and-endpoints.md`` ("Compatibility is protocol
    intersection plus the lock").
    """
    protocol_kinds = getattr(capability, "protocol_kinds", None)
    if not protocol_kinds:
        protocol_kinds = getattr(capability, "provider_kinds", frozenset())
    if ep.protocol_kind not in protocol_kinds:
        return (
            f"endpoint {ep.label!r} speaks {ep.protocol_kind!r}, which "
            f"{capability.label} does not support "
            f"(supports {sorted(protocol_kinds)})"
        )
    if ep.harness_lock and ep.harness_lock != capability.code:
        return (
            f"endpoint {ep.label!r} is locked to the {ep.harness_lock!r} "
            f"harness and cannot be used by {capability.code!r}"
        )
    return f"endpoint {ep.label!r} is not compatible with {capability.code!r}"


def _validate_endpoint_compat(session: Session, capability, endpoint_id: str) -> None:
    """404 for an unknown endpoint, 400 for one that fails the compat gate."""
    try:
        ep = get_endpoint(session, endpoint_id)
    except EndpointNotFound:
        raise HTTPException(404, f"unknown endpoint {endpoint_id!r}")
    if not endpoint_compatible(capability, ep):
        raise HTTPException(400, _compat_message(capability, ep))


def _validate_profile_compat(
    session: Session, capability, endpoint_id: Optional[str], backend_config: Optional[dict],
) -> None:
    """Blocking gate: the primary endpoint and every per-agent endpoint
    reference (``backend_config["agents"][].endpoint_id``) must pass
    :func:`endpoint_compatible` for the profile's backend."""
    if endpoint_id:
        _validate_endpoint_compat(session, capability, endpoint_id)
    for agent in (backend_config or {}).get("agents", []) or []:
        if isinstance(agent, dict) and agent.get("endpoint_id"):
            _validate_endpoint_compat(session, capability, agent["endpoint_id"])


def _model_menu_warnings(
    session: Session,
    endpoint_id: Optional[str],
    default_model: Optional[str],
    backend_config: Optional[dict],
) -> list[str]:
    """Warn-only: model assignments not present in their endpoint's model
    menu don't block the save (vendors serve underdocumented models), but
    are surfaced back to the caller."""
    warnings: list[str] = []

    def _check(eid: Optional[str], model: Optional[str], where: str) -> None:
        if not eid or not model:
            return
        try:
            ep = get_endpoint(session, eid)
        except EndpointNotFound:
            return
        menu = ep.models or []
        if menu and model not in menu:
            warnings.append(
                f"{where}: model {model!r} is not in endpoint {ep.label!r}'s model menu"
            )

    _check(endpoint_id, default_model, "default_model")
    for agent in (backend_config or {}).get("agents", []) or []:
        if not isinstance(agent, dict):
            continue
        agent_endpoint_id = agent.get("endpoint_id") or endpoint_id
        name = agent.get("name") or "agent"
        _check(agent_endpoint_id, agent.get("model"), f"agents[{name!r}]")
    return warnings


def _profile_out(
    profile, box: Optional[ProfileSecretBox], *, warnings: Optional[list[str]] = None,
) -> dict:
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
        "execution_target": getattr(profile, "execution_target", None) or "local",
        "endpoint_id": profile.endpoint_id,
        "backend_config": profile.backend_config or {},
        "claude_credentials": creds_out,
        "claude_binary_path": profile.claude_binary_path,
        "env_keys": env_keys,
        "system_prompt": profile.system_prompt,
        "permission_mode": profile.permission_mode,
        "cc_settings_passthrough": profile.cc_settings_passthrough or {},
        "run_token_scopes": profile.run_token_scopes or [],
        "warnings": warnings or [],
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


def build_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/profiles",
        tags=["profiles"],
        dependencies=[Depends(require_token_cookie_or_bearer(bearer_token))],
    )
    box = ProfileSecretBox(bearer_token) if bearer_token else None

    @router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
    async def create(payload: ProfileCreate, session: Session = Depends(get_session)):
        fields = payload.model_dump()
        creds_in = fields.pop("claude_credentials", None)
        env_in = fields.pop("env", None)
        # Only require / encrypt credentials for backends that actually use them
        # (those consuming the claude_auth field group). omp_rpc and any future
        # non-Claude backend (e.g. opencode) must be creatable without supplying credentials.
        backend = fields.get("backend", DEFAULT_BACKEND)
        if capability_or_default(backend).consumes("claude_auth"):
            fields["claude_credentials"] = _encrypt_credentials_in(
                box, creds_in, None, endpoint_id=fields.get("endpoint_id"),
            )
        else:
            fields.pop("claude_credentials", None)
        env_token = _encrypt_env_in(box, env_in)
        fields["env"] = None if env_token == "__UNCHANGED__" else env_token
        _validate_paths(fields.get("fs_read") or [], field="fs_read")
        _validate_paths(fields.get("fs_write") or [], field="fs_write")
        if fields.get("network_mode") and fields["network_mode"] not in _NETWORK_MODES:
            raise HTTPException(400, f"network_mode must be one of {_NETWORK_MODES}")
        for scope in fields.get("run_token_scopes") or []:
            if scope not in _VALID_RUN_TOKEN_SCOPES:
                raise HTTPException(400, f"unknown run_token_scope {scope!r}")
        capability = capability_or_default(backend)
        endpoint_id = fields.get("endpoint_id")
        backend_config = fields.get("backend_config")
        _validate_profile_compat(session, capability, endpoint_id, backend_config)
        try:
            profile = create_profile(session, **fields)
        except ProfileNameTaken:
            raise HTTPException(409, "name taken")
        warnings = _model_menu_warnings(
            session, profile.endpoint_id, profile.default_model, profile.backend_config,
        )
        return _profile_out(profile, box, warnings=warnings)

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
            effective_endpoint_id = raw.get("endpoint_id", existing.endpoint_id)
            fields["claude_credentials"] = _encrypt_credentials_in(
                box, raw["claude_credentials"], existing.claude_credentials,
                endpoint_id=effective_endpoint_id,
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
        # Re-run the compatibility gate whenever backend, endpoint_id, or
        # backend_config change — e.g. changing backend on a profile whose
        # endpoint no longer passes the gate must 400, not save silently.
        if "backend" in raw or "endpoint_id" in raw or "backend_config" in raw:
            effective_backend = fields.get("backend", existing.backend)
            effective_endpoint_id = fields.get("endpoint_id", existing.endpoint_id)
            effective_backend_config = fields.get("backend_config", existing.backend_config)
            capability = capability_or_default(effective_backend)
            _validate_profile_compat(
                session, capability, effective_endpoint_id, effective_backend_config,
            )
        try:
            profile = update_profile(session, pid, **fields)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        except ProfileNameTaken:
            raise HTTPException(409, "name taken")
        warnings = _model_menu_warnings(
            session, profile.endpoint_id, profile.default_model, profile.backend_config,
        )
        return _profile_out(profile, box, warnings=warnings)

    @router.delete("/{pid}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete(pid: str, session: Session = Depends(get_session)):
        try:
            delete_profile(session, pid)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        return None

    @router.post("/{pid}/copy", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
    async def copy_api(pid: str, session: Session = Depends(get_session)):
        """Clone an existing profile. Name collisions resolve to "<name> (copy)",
        "<name> (copy 2)", etc."""
        try:
            src = get_profile(session, pid)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        existing_names = {p.name for p in list_profiles(session)}
        new_name = _next_copy_name(existing_names, src.name)
        try:
            new_profile = create_profile(session, name=new_name, **_copy_fields(src))
        except ProfileNameTaken:
            raise HTTPException(409, f"name {new_name!r} taken")
        return _profile_out(new_profile, box)

    @router.get("/{pid}/export")
    async def export_api(pid: str, session: Session = Depends(get_session)):
        """JSON export with secrets redacted (never returns plaintext values)."""
        try:
            profile = get_profile(session, pid)
        except ProfileNotFound:
            raise HTTPException(404, "not found")
        return JSONResponse(_profile_export(profile, box))

    @router.post("/import", response_model=ProfileImportResult, status_code=status.HTTP_201_CREATED)
    async def import_api(body: ProfileImport, session: Session = Depends(get_session)):
        """Re-import a nightdesk profile export (JSON body, not multipart).

        Same forbidden-field stripping and allowlist as a fresh create;
        secrets must be re-entered (the export never carries plaintext
        values).
        """
        cleaned, dropped = strip_forbidden_import_fields(body.payload)
        creds_in = cleaned.pop("claude_credentials", None)
        if "env" in cleaned and isinstance(cleaned["env"], dict):
            env_in = cleaned.pop("env")
        else:
            env_in = None
            cleaned.pop("env", None)
        for k in ("id", "created_at", "updated_at"):
            cleaned.pop(k, None)
        allowed_keys = {
            "name", "description", "fs_read", "fs_write", "allowed_tools",
            "denied_tools", "network_mode", "network_allowlist", "secret_keys",
            "default_model", "backend", "endpoint_id", "backend_config", "claude_binary_path",
            "system_prompt", "permission_mode", "cc_settings_passthrough",
            "run_token_scopes",
        }
        fields: dict[str, Any] = {k: v for k, v in cleaned.items() if k in allowed_keys}
        if body.name:
            fields["name"] = body.name
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
        return ProfileImportResult(id=profile.id, dropped_fields=dropped)

    @router.post(
        "/import-from-cc", response_model=ProfileImportResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_from_cc_api(
        body: ProfileImportFromCC, session: Session = Depends(get_session),
    ):
        """Translate a Claude Code ``settings.json`` payload into a profile."""
        _, dropped = strip_forbidden_import_fields(body.settings)
        fallback_name = body.name or "Imported from Claude Code"
        try:
            fields = translate_cc_settings(body.settings, name=fallback_name)
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
        return ProfileImportResult(id=profile.id, dropped_fields=dropped)

    return router
