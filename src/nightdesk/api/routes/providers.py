"""Provider + endpoint management API (JSON, ``/api/v1/providers`` and
``/api/v1/provider-endpoints``).

See ``docs/design/providers-and-endpoints.md`` ("Layer 1"). Credentials and
the ``extra`` blob are Fernet-encrypted with ``ProfileSecretBox`` (same
scheme as ``profiles.claude_credentials``) before persistence; responses
never return plaintext, only ``credential_set`` / ``extra_set`` flags.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from nightdesk.domain import scopes as sc
from nightdesk.api.schemas import (
    CatalogOfferingOut,
    EndpointCreate,
    EndpointOut,
    EndpointUpdate,
    ProviderCreate,
    ProviderOut,
    ProviderRotateCredential,
    ProviderRotateResult,
    ProviderUpdate,
)
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.provider_catalog import catalog as offering_catalog
from nightdesk.domain.providers import (
    CREDENTIAL_SOURCES,
    PROTOCOL_KINDS,
    EndpointInUse,
    EndpointNotFound,
    ProviderInUse,
    ProviderNameTaken,
    ProviderNotFound,
    UnknownCredentialSource,
    UnknownProtocolKind,
    create_endpoint,
    create_provider,
    delete_endpoint,
    delete_provider,
    get_endpoint,
    get_provider,
    list_providers,
    resolve_endpoint,
    update_endpoint,
    update_provider,
)


_PULL_MODELS_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _endpoint_out(ep) -> dict:
    return {
        "id": ep.id,
        "provider_id": ep.provider_id,
        "label": ep.label,
        "protocol_kind": ep.protocol_kind,
        "base_url": ep.base_url,
        "credential_source": ep.credential_source,
        "credential_set": bool(ep.credential),
        "harness_lock": ep.harness_lock,
        "default_model": ep.default_model,
        "models": list(ep.models or []),
        "models_pulled_at": ep.models_pulled_at,
        "extra_set": bool(ep.extra),
        "created_at": ep.created_at,
        "updated_at": ep.updated_at,
    }


def _provider_out(p) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "vendor": p.vendor,
        "endpoints": [_endpoint_out(ep) for ep in p.endpoints],
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _catalog_out() -> list[dict]:
    return [
        {
            "key": o.key,
            "label": o.label,
            "vendor": o.vendor,
            "credential_source": o.credential_source,
            "credential_hint": o.credential_hint,
            "description": o.description,
            "suggested_name": o.suggested_name,
            "endpoints": [
                {
                    "label": e.label,
                    "protocol_kind": e.protocol_kind,
                    "base_url": e.base_url,
                    "harness_lock": e.harness_lock,
                    "default_model": e.default_model,
                }
                for e in o.endpoints
            ],
        }
        for o in offering_catalog()
    ]


# Credential-source families that must never appear together in a single
# provider create call: pasting a secret and pointing at a credential file
# are two different setup flows, and mixing them means at least one
# endpoint's declared mode has nothing to back it. See
# docs/design/providers-and-endpoints.md and the catalog module docstring —
# every catalog offering already locks its endpoints to one mode; this is
# the server-side backstop for hand-built (custom) create payloads.
_SECRET_CREDENTIAL_SOURCES = frozenset({"api_key", "env_var"})
_FILE_CREDENTIAL_SOURCES = frozenset({"oauth_file", "subscription_file"})


def _check_single_credential_mode(endpoints: list[EndpointCreate]) -> None:
    sources = {ep.credential_source for ep in endpoints}
    if sources & _SECRET_CREDENTIAL_SOURCES and sources & _FILE_CREDENTIAL_SOURCES:
        raise HTTPException(
            400,
            "endpoints must share a single credential mode: choose either a "
            f"pasted credential ({sorted(sources & _SECRET_CREDENTIAL_SOURCES)}) or a "
            f"credential file path ({sorted(sources & _FILE_CREDENTIAL_SOURCES)}), not both. "
            "Register them as separate providers instead.",
        )


# ---------------------------------------------------------------------------
# Model discovery (pull-models)
# ---------------------------------------------------------------------------


class PullModelsError(Exception):
    """A protocol has no list operation, or the endpoint is unusable."""


def _list_request(resolved) -> tuple[str, dict[str, str]]:
    """``(url, headers)`` for a model-list GET, per protocol family."""
    protocol = resolved.protocol_kind
    cred = resolved.credential
    if protocol in ("openai", "openai_compat"):
        base = (resolved.base_url or "").rstrip("/")
        if not base:
            raise PullModelsError("endpoint has no base_url to list models from")
        headers = {"Authorization": f"Bearer {cred}"} if cred else {}
        return f"{base}/models", headers
    if protocol == "openrouter":
        base = (resolved.base_url or "").rstrip("/")
        url = f"{base}/models" if base else "https://openrouter.ai/api/v1/models"
        headers = {"Authorization": f"Bearer {cred}"} if cred else {}
        return url, headers
    if protocol in ("anthropic", "anthropic_compat"):
        base = (resolved.base_url or "https://api.anthropic.com").rstrip("/")
        headers = {"x-api-key": cred} if cred else {}
        return f"{base}/v1/models", headers
    if protocol == "ollama":
        base = (resolved.base_url or "http://localhost:11434").rstrip("/")
        return f"{base}/api/tags", {}
    if protocol == "openai_codex":
        raise PullModelsError("openai_codex has no list operation; curate manually")
    raise PullModelsError(f"unknown protocol_kind {protocol!r}")


def _parse_models(protocol: str, payload: Any) -> list[str]:
    """Defensively parse a model-list response into a flat list of ids."""
    if protocol == "ollama":
        items = payload.get("models") if isinstance(payload, dict) else None
        out = []
        for it in items or []:
            if isinstance(it, dict) and it.get("name"):
                out.append(it["name"])
        return out
    data = payload.get("data") if isinstance(payload, dict) else None
    out = []
    for it in data or []:
        if isinstance(it, dict) and it.get("id"):
            out.append(it["id"])
    return out


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_router(get_session, bearer_token: str, scoped) -> APIRouter:
    router = APIRouter(
        tags=["providers"],
        dependencies=[Depends(scoped(sc.PROVIDERS_READ))],
    )
    write = Depends(scoped(sc.PROVIDERS_WRITE))
    box = ProfileSecretBox(bearer_token) if bearer_token else None

    def _encrypt_optional(value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None
        if box is None:
            raise HTTPException(
                500,
                "credential cannot be stored: bearer_token is empty so no "
                "encryption key is available",
            )
        return box.encrypt(value)

    def _endpoint_create_fields(
        ep_in: EndpointCreate, *, seeded_credential: Optional[str],
    ) -> dict[str, Any]:
        credential_value = ep_in.credential_value
        if (
            credential_value is None
            and ep_in.credential_source != "none"
            and seeded_credential is not None
        ):
            credential_value = seeded_credential
        return {
            "label": ep_in.label,
            "protocol_kind": ep_in.protocol_kind,
            "base_url": ep_in.base_url,
            "credential_source": ep_in.credential_source,
            "credential": _encrypt_optional(credential_value),
            "harness_lock": ep_in.harness_lock,
            "default_model": ep_in.default_model,
            "models": ep_in.models,
            "extra": _encrypt_optional(ep_in.extra),
        }

    # -- providers -----------------------------------------------------

    @router.get("/api/v1/providers", response_model=list[ProviderOut])
    async def list_providers_api(session: Session = Depends(get_session)):
        return [_provider_out(p) for p in list_providers(session)]

    @router.post("/api/v1/providers", response_model=ProviderOut, status_code=status.HTTP_201_CREATED, dependencies=[write])
    async def create_provider_api(payload: ProviderCreate, session: Session = Depends(get_session)):
        for ep_in in payload.endpoints:
            if ep_in.protocol_kind not in PROTOCOL_KINDS:
                raise HTTPException(400, f"unknown protocol_kind {ep_in.protocol_kind!r}")
            if ep_in.credential_source not in CREDENTIAL_SOURCES:
                raise HTTPException(400, f"unknown credential_source {ep_in.credential_source!r}")
        _check_single_credential_mode(payload.endpoints)
        try:
            provider = create_provider(session, name=payload.name, vendor=payload.vendor)
        except ProviderNameTaken:
            raise HTTPException(409, "name taken")
        for ep_in in payload.endpoints:
            fields = _endpoint_create_fields(ep_in, seeded_credential=payload.credential_value)
            create_endpoint(session, provider_id=provider.id, **fields)
        session.refresh(provider)
        return _provider_out(provider)

    # Registered before "/{pid}" so "catalog" is never captured by the param route.
    @router.get("/api/v1/providers/catalog", response_model=list[CatalogOfferingOut])
    async def catalog_api():
        return _catalog_out()

    @router.get("/api/v1/providers/{pid}", response_model=ProviderOut)
    async def show_provider_api(pid: str, session: Session = Depends(get_session)):
        try:
            return _provider_out(get_provider(session, pid))
        except ProviderNotFound:
            raise HTTPException(404, "not found")

    @router.patch("/api/v1/providers/{pid}", response_model=ProviderOut, dependencies=[write])
    async def update_provider_api(
        pid: str, payload: ProviderUpdate, session: Session = Depends(get_session),
    ):
        raw = payload.model_dump(exclude_unset=True)
        try:
            provider = update_provider(session, pid, **raw)
        except ProviderNotFound:
            raise HTTPException(404, "not found")
        except ProviderNameTaken:
            raise HTTPException(409, "name taken")
        return _provider_out(provider)

    @router.delete("/api/v1/providers/{pid}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[write])
    async def delete_provider_api(pid: str, session: Session = Depends(get_session)):
        try:
            delete_provider(session, pid)
        except ProviderNotFound:
            raise HTTPException(404, "not found")
        except ProviderInUse:
            raise HTTPException(409, "provider is in use")
        return None

    @router.post(
        "/api/v1/providers/{pid}/endpoints", response_model=EndpointOut,
        status_code=status.HTTP_201_CREATED, dependencies=[write],
    )
    async def create_endpoint_api(
        pid: str, payload: EndpointCreate, session: Session = Depends(get_session),
    ):
        try:
            get_provider(session, pid)
        except ProviderNotFound:
            raise HTTPException(404, "not found")
        fields = _endpoint_create_fields(payload, seeded_credential=None)
        try:
            ep = create_endpoint(session, provider_id=pid, **fields)
        except (UnknownProtocolKind, UnknownCredentialSource) as exc:
            raise HTTPException(400, str(exc))
        return _endpoint_out(ep)

    @router.post("/api/v1/providers/{pid}/rotate-credential", response_model=ProviderRotateResult, dependencies=[write])
    async def rotate_credential_api(
        pid: str, payload: ProviderRotateCredential, session: Session = Depends(get_session),
    ):
        try:
            provider = get_provider(session, pid)
        except ProviderNotFound:
            raise HTTPException(404, "not found")
        encrypted = _encrypt_optional(payload.credential_value)
        count = 0
        for ep in provider.endpoints:
            if ep.credential_source == "api_key":
                update_endpoint(session, ep.id, credential=encrypted)
                count += 1
        return {"rotated": count}

    # -- endpoints -------------------------------------------------------

    @router.patch("/api/v1/provider-endpoints/{eid}", response_model=EndpointOut, dependencies=[write])
    async def update_endpoint_api(
        eid: str, payload: EndpointUpdate, session: Session = Depends(get_session),
    ):
        raw = payload.model_dump(exclude_unset=True)
        fields: dict[str, Any] = {}
        for key in (
            "label", "protocol_kind", "base_url", "credential_source",
            "harness_lock", "default_model", "models",
        ):
            if key in raw:
                fields[key] = raw[key]
        if "credential_value" in raw:
            fields["credential"] = _encrypt_optional(raw["credential_value"])
        if "extra" in raw:
            fields["extra"] = _encrypt_optional(raw["extra"])
        try:
            ep = update_endpoint(session, eid, **fields)
        except EndpointNotFound:
            raise HTTPException(404, "not found")
        except (UnknownProtocolKind, UnknownCredentialSource) as exc:
            raise HTTPException(400, str(exc))
        return _endpoint_out(ep)

    @router.delete("/api/v1/provider-endpoints/{eid}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[write])
    async def delete_endpoint_api(eid: str, session: Session = Depends(get_session)):
        try:
            delete_endpoint(session, eid)
        except EndpointNotFound:
            raise HTTPException(404, "not found")
        except EndpointInUse:
            raise HTTPException(409, "endpoint is in use")
        return None

    @router.post("/api/v1/provider-endpoints/{eid}/pull-models", response_model=EndpointOut, dependencies=[write])
    async def pull_models_api(eid: str, session: Session = Depends(get_session)):
        try:
            get_endpoint(session, eid)
        except EndpointNotFound:
            raise HTTPException(404, "not found")
        resolved = resolve_endpoint(session, eid, box)
        if resolved is None:
            raise HTTPException(404, "not found")
        try:
            url, headers = _list_request(resolved)
        except PullModelsError as exc:
            raise HTTPException(400, str(exc))
        try:
            with httpx.Client(timeout=_PULL_MODELS_TIMEOUT) as http_client:
                resp = http_client.get(url, headers={"Accept": "application/json", **headers})
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller as 502
            raise HTTPException(502, f"model pull failed: {exc}")
        models = _parse_models(resolved.protocol_kind, payload)
        ep = update_endpoint(
            session, eid, models=models, models_pulled_at=datetime.now(timezone.utc),
        )
        return _endpoint_out(ep)

    return router
