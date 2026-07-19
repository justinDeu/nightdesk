"""Provider + endpoint management API (JSON, ``/api/v1/providers`` and
``/api/v1/provider-endpoints``).

See ``docs/design/providers-and-endpoints.md`` ("Layer 1"). Credentials and
the ``extra`` blob are Fernet-encrypted with ``ProfileSecretBox`` (same
scheme as ``profiles.claude_credentials``) before persistence; responses
never return plaintext, only ``credential_set`` / ``extra_set`` flags.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
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
    EndpointUsageOut,
    ProtocolInfoOut,
    ProviderCreate,
    ProviderOut,
    ProviderRotateCredential,
    ProviderRotateResult,
    ProviderUpdate,
    SubscriptionUsageOut,
    UsageWindowOut,
)
from nightdesk.backends.claude_code import _extract_subscription_token
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
    supports_model_list,
    update_endpoint,
    update_provider,
)


_PULL_MODELS_TIMEOUT = 10.0

log = logging.getLogger(__name__)

# The ChatGPT backend endpoint Codex CLI (0.144.x) calls to fetch its model
# catalog — confirmed by inspecting the CLI's Rust binary: the literal base
# string ``https://chatgpt.com/backend-api/codex`` appears standalone in the
# vendored ``codex-api/src/endpoint/models.rs`` build, and probing
# ``.../codex/models`` unauthenticated returns a JSON 401 from the real auth
# middleware ("Could not parse your authentication token"), while sibling
# nonexistent paths under the same prefix return a generic 403 HTML page.
# Same surface Codex CLI itself uses with the stored OAuth access token.
_CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"

# Codex CLI's own local cache of the last successful fetch (written by the
# CLI, read here only as a fallback — see ``_codex_cache_fallback_models``).
_CODEX_MODELS_CACHE_PATH = "~/.codex/models_cache.json"

# "fresh enough" to serve on a live-fetch failure. Generous on purpose: a
# stale local list beats a hard error, and this cache only feeds a manual
# "Refresh models" click, not anything time-critical.
_CODEX_MODELS_CACHE_MAX_AGE = timedelta(days=7)


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
                    "models": list(e.models),
                }
                for e in o.endpoints
            ],
        }
        for o in offering_catalog()
    ]


def _protocols_out() -> list[dict]:
    return [
        {"key": kind, "supports_model_list": supports_model_list(kind)}
        for kind in PROTOCOL_KINDS
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


def _catalog_credential_hint(
    vendor: str, protocol_kind: str, credential_source: str,
) -> Optional[str]:
    """The seeded catalog offering's ``credential_hint`` for an endpoint
    matching ``vendor`` + ``protocol_kind`` + ``credential_source``, or
    ``None`` when no catalog offering matches — the signal a create-provider
    call distinguishes "from the catalog wizard" (default the hint) from
    "hand-built/custom" (reject an empty file-path credential outright).
    """
    for offering in offering_catalog():
        if offering.vendor != vendor or offering.credential_source != credential_source:
            continue
        if any(ep.protocol_kind == protocol_kind for ep in offering.endpoints):
            return offering.credential_hint
    return None


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
        return _CODEX_MODELS_URL, _codex_auth_headers(cred)
    raise PullModelsError(f"unknown protocol_kind {protocol!r}")


def _codex_auth_headers(credential: Optional[str]) -> dict[str, str]:
    """Headers for a Codex model-list request, built from the resolved
    ``auth.json`` blob (an ``oauth_file`` credential is the file's raw text —
    see ``_resolve_credential``).

    Fail-soft like ``_parse_codex_oauth`` in ``backends/opencode_config.py``:
    any parse/shape problem yields an empty header set rather than raising,
    so a bad/missing credential surfaces as a 401 from the live fetch (which
    then falls through to the local cache) instead of a crash here.
    """
    if not credential:
        return {}
    try:
        data = json.loads(credential)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return {}
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        return {}
    headers = {"Authorization": f"Bearer {access}", "originator": "codex_cli_rs"}
    account_id = tokens.get("account_id")
    if isinstance(account_id, str) and account_id:
        headers["chatgpt-account-id"] = account_id
    return headers


def _parse_models(protocol: str, payload: Any) -> list[str]:
    """Defensively parse a model-list response into a flat list of ids."""
    if protocol == "ollama":
        items = payload.get("models") if isinstance(payload, dict) else None
        out = []
        for it in items or []:
            if isinstance(it, dict) and it.get("name"):
                out.append(it["name"])
        return out
    if protocol == "openai_codex":
        return _codex_slugs(payload.get("models") if isinstance(payload, dict) else None)
    data = payload.get("data") if isinstance(payload, dict) else None
    out = []
    for it in data or []:
        if isinstance(it, dict) and it.get("id"):
            out.append(it["id"])
    return out


def _codex_slugs(items: Any) -> list[str]:
    """Filter a Codex model list (either the live API response's ``models``
    array or the local cache's) down to publicly-listable slugs.

    ``visibility`` distinguishes the public menu from internal-only entries
    (e.g. ``codex-auto-review``) that must never appear as a user-selectable
    model.
    """
    out = []
    for it in items or []:
        if isinstance(it, dict) and it.get("visibility") == "list" and it.get("slug"):
            out.append(it["slug"])
    return out


def _codex_cache_fallback_models() -> Optional[list[str]]:
    """Slugs from Codex CLI's local ``~/.codex/models_cache.json``, if the
    file exists, parses, and isn't too stale. Returns ``None`` when no
    usable fallback is available (missing file, bad JSON, or ``fetched_at``
    older than ``_CODEX_MODELS_CACHE_MAX_AGE``) so the caller can tell "no
    fallback" apart from "fallback yielded zero listable models".
    """
    path = os.path.expanduser(_CODEX_MODELS_CACHE_PATH)
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.info("codex models cache fallback: %s unusable: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    fetched_at = data.get("fetched_at")
    if isinstance(fetched_at, str):
        try:
            fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        except ValueError:
            log.info("codex models cache fallback: unparseable fetched_at %r", fetched_at)
            return None
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - fetched
        if age > _CODEX_MODELS_CACHE_MAX_AGE:
            log.info("codex models cache fallback: cache is stale (age=%s)", age)
            return None
    return _codex_slugs(data.get("models"))


# ---------------------------------------------------------------------------
# Subscription usage (rate-limit windows)
# ---------------------------------------------------------------------------

# Which (protocol_kind, credential_source) pairs expose a usage endpoint.
_USAGE_SOURCES = frozenset({
    ("anthropic", "subscription_file"),
    ("openai_codex", "oauth_file"),
})

_USAGE_TIMEOUT = 10.0

# TTL for the in-process usage cache. Anthropic rate-limits aggressive polling
# of the usage endpoint; 180s is the documented safe floor and matches the
# frontend's poll cadence.
_USAGE_CACHE_TTL = 180.0

_ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# Anthropic 429s hard without a recognized Claude Code User-Agent.
_CLAUDE_CODE_USER_AGENT = "claude-code/2.0.32"

# endpoint id -> (monotonic fetched-at, last good EndpointUsageOut). Only
# successful fetches are cached; on a later error the cached entry is served
# with ``error`` set rather than being overwritten.
_usage_cache: dict[str, tuple[float, EndpointUsageOut]] = {}


class UsageFetchError(Exception):
    """The endpoint has no usable credential, or the upstream fetch failed."""


def _severity_for(percent: float) -> str:
    """Threshold severity for vendors/paths that don't supply one."""
    if percent >= 90:
        return "critical"
    if percent >= 75:
        return "warning"
    return "normal"


def _parse_usage_ts(value: Any) -> Optional[datetime]:
    """ISO-8601 string -> aware UTC datetime, or None on anything unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _anthropic_window_label(entry: dict) -> str:
    """Human label for an Anthropic ``limits[]`` entry from its kind/scope."""
    kind = entry.get("kind") or ""
    if kind == "session":
        return "5h"
    if kind == "weekly_all":
        return "Weekly"
    scope = entry.get("scope")
    model = scope.get("model") if isinstance(scope, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    if isinstance(name, str) and name:
        return f"Weekly · {name}"
    if kind == "weekly_opus":
        return "Weekly · Opus"
    if kind == "weekly_sonnet":
        return "Weekly · Sonnet"
    if kind.startswith("weekly"):
        return "Weekly"
    return kind or "Usage"


def _parse_anthropic_usage(payload: Any) -> tuple[Optional[str], list[UsageWindowOut]]:
    """Normalize ``/api/oauth/usage`` into windows.

    Prefers the ``limits[]`` array (richer: per-kind labels + a supplied
    severity), falling back to the top-level ``five_hour`` / ``seven_day``
    blocks when ``limits`` is absent. Anthropic exposes no plan field here, so
    ``plan`` is always None.
    """
    if not isinstance(payload, dict):
        return None, []
    windows: list[UsageWindowOut] = []
    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            percent = entry.get("percent")
            if percent is None:
                continue
            severity = entry.get("severity") or _severity_for(float(percent))
            windows.append(UsageWindowOut(
                label=_anthropic_window_label(entry),
                used_percent=float(percent),
                resets_at=_parse_usage_ts(entry.get("resets_at")),
                severity=severity,
            ))
        return None, windows
    for key, label in (("five_hour", "5h"), ("seven_day", "Weekly")):
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        util = block.get("utilization")
        if util is None:
            continue
        windows.append(UsageWindowOut(
            label=label,
            used_percent=float(util),
            resets_at=_parse_usage_ts(block.get("resets_at")),
            severity=_severity_for(float(util)),
        ))
    return None, windows


def _codex_window_label(seconds: Any) -> str:
    """Humanize a Codex ``limit_window_seconds`` into a window label."""
    if seconds == 604800:
        return "Weekly"
    if seconds == 18000:
        return "5h"
    if isinstance(seconds, (int, float)) and seconds > 0:
        hours = seconds / 3600
        if hours >= 24 and hours % 24 == 0:
            return f"{int(hours // 24)}d"
        return f"{int(round(hours))}h"
    return "Window"


def _codex_window(window: Any, label: Optional[str]) -> Optional[UsageWindowOut]:
    """One Codex rate-limit window -> UsageWindowOut, or None when absent."""
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    if used is None:
        return None
    reset_at = window.get("reset_at")
    resets_at = (
        datetime.fromtimestamp(reset_at, tz=timezone.utc)
        if isinstance(reset_at, (int, float))
        else None
    )
    return UsageWindowOut(
        label=label or _codex_window_label(window.get("limit_window_seconds")),
        used_percent=float(used),
        resets_at=resets_at,
        severity=_severity_for(float(used)),
    )


def _parse_codex_usage(payload: Any) -> tuple[Optional[str], list[UsageWindowOut]]:
    """Normalize ``/backend-api/wham/usage`` into (plan, windows)."""
    if not isinstance(payload, dict):
        return None, []
    plan = payload.get("plan_type")
    plan = plan if isinstance(plan, str) and plan else None
    windows: list[UsageWindowOut] = []
    rate_limit = payload.get("rate_limit")
    if isinstance(rate_limit, dict):
        for key in ("primary_window", "secondary_window"):
            win = _codex_window(rate_limit.get(key), None)
            if win is not None:
                windows.append(win)
    for extra in payload.get("additional_rate_limits") or []:
        if not isinstance(extra, dict):
            continue
        name = extra.get("limit_name")
        label = name if isinstance(name, str) and name else None
        sub = extra.get("rate_limit")
        if not isinstance(sub, dict):
            continue
        for key in ("primary_window", "secondary_window"):
            win = _codex_window(sub.get(key), label)
            if win is not None:
                windows.append(win)
    return plan, windows


def _anthropic_usage_request(credential: Optional[str]) -> tuple[str, dict[str, str]]:
    token = _extract_subscription_token(credential) if credential else None
    if not token:
        raise UsageFetchError("no Claude subscription token available")
    return _ANTHROPIC_USAGE_URL, {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": _CLAUDE_CODE_USER_AGENT,
        "Accept": "application/json",
    }


def _codex_usage_request(credential: Optional[str]) -> tuple[str, dict[str, str]]:
    access: Optional[str] = None
    account_id: Optional[str] = None
    if credential:
        try:
            data = json.loads(credential)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            tokens = data.get("tokens")
            if isinstance(tokens, dict):
                access = tokens.get("access_token")
                account_id = tokens.get("account_id")
    if not isinstance(access, str) or not access:
        raise UsageFetchError("no Codex access token available")
    headers = {
        "Authorization": f"Bearer {access}",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": "Mozilla/5.0",
    }
    if isinstance(account_id, str) and account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return _CODEX_USAGE_URL, headers


def _fetch_usage(resolved) -> tuple[Optional[str], list[UsageWindowOut]]:
    """Live-fetch + normalize usage for one resolved endpoint. Raises
    ``UsageFetchError`` (missing credential) or an httpx error on failure."""
    protocol = resolved.protocol_kind
    if protocol == "anthropic":
        url, headers = _anthropic_usage_request(resolved.credential)
        parse = _parse_anthropic_usage
    elif protocol == "openai_codex":
        url, headers = _codex_usage_request(resolved.credential)
        parse = _parse_codex_usage
    else:
        raise UsageFetchError(f"no usage endpoint for protocol {protocol!r}")
    with httpx.Client(timeout=_USAGE_TIMEOUT) as http_client:
        resp = http_client.get(url, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    return parse(payload)


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

    def _endpoint_credential_value(
        ep_in: EndpointCreate, *, seeded_credential: Optional[str],
    ) -> Optional[str]:
        credential_value = ep_in.credential_value
        if (
            credential_value is None
            and ep_in.credential_source != "none"
            and seeded_credential is not None
        ):
            credential_value = seeded_credential
        return credential_value

    def _endpoint_create_fields(
        ep_in: EndpointCreate, *, credential_value: Optional[str],
    ) -> dict[str, Any]:
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
        # Belt-and-braces backstop for file-path credential sources
        # (oauth_file / subscription_file): a blank value is never a valid
        # config (see the frontend fix in ProvidersSection.tsx pickOffering,
        # which now seeds the catalog hint as a real editable value instead
        # of a placeholder — this covers hand-built API calls and any UI
        # regression). A create that matches a known catalog offering
        # defaults the empty path to that offering's credential_hint; a
        # genuinely custom create with no such match is rejected outright
        # rather than silently persisting a NULL credential that only
        # surfaces as an opaque runtime auth error turns later.
        resolved_credentials: dict[int, Optional[str]] = {}
        for i, ep_in in enumerate(payload.endpoints):
            credential_value = _endpoint_credential_value(
                ep_in, seeded_credential=payload.credential_value)
            if ep_in.credential_source in _FILE_CREDENTIAL_SOURCES and not (
                credential_value or ""
            ).strip():
                hint = _catalog_credential_hint(
                    payload.vendor, ep_in.protocol_kind, ep_in.credential_source)
                if hint is None:
                    raise HTTPException(
                        400,
                        f"endpoint {ep_in.label or ep_in.protocol_kind!r} needs a "
                        f"credential file path ({ep_in.credential_source})",
                    )
                credential_value = hint
            resolved_credentials[i] = credential_value
        try:
            provider = create_provider(session, name=payload.name, vendor=payload.vendor)
        except ProviderNameTaken:
            raise HTTPException(409, "name taken")
        for i, ep_in in enumerate(payload.endpoints):
            fields = _endpoint_create_fields(
                ep_in, credential_value=resolved_credentials[i])
            create_endpoint(session, provider_id=provider.id, **fields)
        session.refresh(provider)
        return _provider_out(provider)

    # Registered before "/{pid}" so "catalog" is never captured by the param route.
    @router.get("/api/v1/providers/catalog", response_model=list[CatalogOfferingOut])
    async def catalog_api():
        return _catalog_out()

    # Registered before "/{pid}" so "protocols" is never captured by the param route.
    @router.get("/api/v1/providers/protocols", response_model=list[ProtocolInfoOut])
    async def protocols_api():
        return _protocols_out()

    def _endpoint_usage(session: Session, provider, ep) -> EndpointUsageOut:
        """Usage for one subscription/OAuth endpoint, cache-first.

        A fresh cache hit skips the outbound call entirely. Otherwise a live
        fetch either refreshes the cache or — on any failure — degrades to the
        last cached result (with ``error`` set) or an empty errored entry, so
        the route never 5xxs because a vendor is unreachable.
        """
        now = time.monotonic()
        cached = _usage_cache.get(ep.id)
        if cached is not None and now - cached[0] < _USAGE_CACHE_TTL:
            return cached[1]
        base = dict(
            provider_id=provider.id,
            provider_name=provider.name,
            endpoint_id=ep.id,
            endpoint_label=ep.label,
            protocol_kind=ep.protocol_kind,
        )
        try:
            resolved = resolve_endpoint(session, ep.id, box)
            if resolved is None:
                raise UsageFetchError("endpoint could not be resolved")
            plan, windows = _fetch_usage(resolved)
            result = EndpointUsageOut(
                **base, plan=plan, windows=windows,
                fetched_at=datetime.now(timezone.utc), error=None,
            )
            _usage_cache[ep.id] = (now, result)
            return result
        except Exception as exc:  # noqa: BLE001 — vendor down must not 5xx
            message = str(exc) or exc.__class__.__name__
            log.warning("usage fetch for endpoint %s failed: %s", ep.id, message)
            if cached is not None:
                return cached[1].model_copy(update={"error": message})
            return EndpointUsageOut(
                **base, plan=None, windows=[],
                fetched_at=datetime.now(timezone.utc), error=message,
            )

    # Registered before ``/{pid}`` so the literal path isn't captured as a
    # provider id (same ordering guard as ``catalog`` / ``protocols`` above).
    # Read-gated by the router-level ``PROVIDERS_READ`` dependency, like every
    # other GET here.
    @router.get("/api/v1/providers/usage", response_model=SubscriptionUsageOut)
    async def providers_usage_api(session: Session = Depends(get_session)):
        entries: list[EndpointUsageOut] = []
        for provider in list_providers(session):
            for ep in provider.endpoints:
                if (ep.protocol_kind, ep.credential_source) in _USAGE_SOURCES:
                    entries.append(_endpoint_usage(session, provider, ep))
        return SubscriptionUsageOut(endpoints=entries)

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
        credential_value = _endpoint_credential_value(payload, seeded_credential=None)
        fields = _endpoint_create_fields(payload, credential_value=credential_value)
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

        payload: Any = None
        fetch_error: Optional[Exception] = None
        try:
            with httpx.Client(timeout=_PULL_MODELS_TIMEOUT) as http_client:
                resp = http_client.get(url, headers={"Accept": "application/json", **headers})
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — may still be recoverable via cache below
            fetch_error = exc

        if fetch_error is None:
            models = _parse_models(resolved.protocol_kind, payload)
            log.info(
                "pull-models %s: served %d models from live fetch (%s)",
                eid, len(models), resolved.protocol_kind,
            )
        elif resolved.protocol_kind == "openai_codex":
            # Better a slightly stale local Codex CLI cache than a hard
            # error — the live fetch needing a fresh access token, network
            # access, etc. is more fragile than reading a file the CLI
            # already maintains.
            cached = _codex_cache_fallback_models()
            if cached is None:
                raise HTTPException(502, f"model pull failed: {fetch_error}")
            models = cached
            log.warning(
                "pull-models %s: live fetch failed (%s); served %d models from "
                "local codex cache fallback (%s)",
                eid, fetch_error, len(models), _CODEX_MODELS_CACHE_PATH,
            )
        else:
            raise HTTPException(502, f"model pull failed: {fetch_error}")

        ep = update_endpoint(
            session, eid, models=models, models_pulled_at=datetime.now(timezone.utc),
        )
        return _endpoint_out(ep)

    return router
