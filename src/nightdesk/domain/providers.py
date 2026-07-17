"""Provider + ProviderEndpoint entities — vendor identity and inference surfaces.

See ``docs/design/providers-and-endpoints.md`` for the full design. In short:
a :class:`~nightdesk.db.models.Provider` is a vendor identity (pricing anchor);
each :class:`~nightdesk.db.models.ProviderEndpoint` it owns carries its own
protocol, base URL, credential, credential source, optional harness lock, and
model menu. Credentials live on the endpoint, not the provider, because one
vendor's surfaces can authenticate differently (Anthropic: API key vs
subscription file; OpenAI: API key vs Codex OAuth).

``ResolvedEndpoint`` is the run-time, decrypted view handed to a backend's
launch step. It is deliberately stdlib-only (no SQLAlchemy, no nightdesk
imports beyond stdlib) so the backends package can depend on it without
pulling in the database layer.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nightdesk.db.models import Profile, Provider, ProviderEndpoint
from nightdesk.domain.profile_secrets import ProfileSecretBox


log = logging.getLogger(__name__)


# The API shapes an endpoint may speak. Orthogonal to vendor and to
# credential source — see the design doc's "Distinctions" section.
PROTOCOL_KINDS: tuple[str, ...] = (
    "anthropic",
    "anthropic_compat",
    "openai",
    "openai_compat",
    "openai_codex",
    "openrouter",
    "ollama",
)

# How an endpoint authenticates. Independent of protocol_kind.
CREDENTIAL_SOURCES: tuple[str, ...] = (
    "api_key",
    "oauth_file",
    "subscription_file",
    "env_var",
    "none",
)

# Protocols whose surface has no model-list operation to pull from — a known,
# permanent property of the protocol, not a runtime failure. ``openai_codex``
# talks to the ChatGPT/Codex subscription surface (Responses-shaped), which
# exposes no ``/models`` endpoint; its model menu is always curated by hand
# (optionally seeded with catalog defaults — see ``provider_catalog``).
# Read by the pull-models route (fails fast with a guidance message instead
# of attempting a request) and surfaced to the frontend via
# ``GET /api/v1/providers/protocols`` so it can disable "Refresh models" and
# foreground the manual editor instead of hard-coding the protocol name.
PROTOCOLS_WITHOUT_MODEL_LIST: frozenset[str] = frozenset({"openai_codex"})


def supports_model_list(protocol_kind: str) -> bool:
    """Whether ``protocol_kind`` exposes a model-list operation to pull from."""
    return protocol_kind not in PROTOCOLS_WITHOUT_MODEL_LIST


# ---------------------------------------------------------------------------
# Run-time views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedEndpoint:
    """Decrypted, run-time view of a ProviderEndpoint handed to a backend.

    ``credential`` is the resolved plaintext secret (or None when the row has
    none, the reference can't be read, or decryption failed — resolution never
    raises so a backend that can fall back to ambient env/auth still runs).
    """

    id: str
    label: str
    protocol_kind: str
    base_url: Optional[str]
    credential: Optional[str]
    credential_source: str
    extra: dict = field(default_factory=dict)
    default_model: Optional[str] = None
    models: list[str] = field(default_factory=list)
    provider_id: str = ""
    provider_name: str = ""
    vendor: str = ""
    # Harness code exclusively allowed to use this endpoint (e.g. a Claude
    # subscription endpoint locks to "claude_sdk"), or None. Carried through
    # to the run-time view so the worker's compatibility gate (and any
    # renderer that cares) sees it without a second DB round-trip.
    harness_lock: Optional[str] = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProviderNotFound(Exception):
    pass


class ProviderNameTaken(Exception):
    pass


class ProviderInUse(Exception):
    """Raised when deleting a provider whose endpoints are still referenced."""


class EndpointNotFound(Exception):
    pass


class EndpointInUse(Exception):
    """Raised when deleting an endpoint still referenced by a profile."""


class UnknownProtocolKind(Exception):
    pass


class UnknownCredentialSource(Exception):
    pass


def _validate_protocol_kind(kind: str) -> None:
    if kind not in PROTOCOL_KINDS:
        raise UnknownProtocolKind(kind)


def _validate_credential_source(source: str) -> None:
    if source not in CREDENTIAL_SOURCES:
        raise UnknownCredentialSource(source)


# ---------------------------------------------------------------------------
# Provider CRUD
# ---------------------------------------------------------------------------


def create_provider(session: Session, **fields) -> Provider:
    p = Provider(**fields)
    session.add(p)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ProviderNameTaken(fields.get("name")) from exc
    session.refresh(p)
    return p


def get_provider(session: Session, provider_id: str) -> Provider:
    p = session.get(Provider, provider_id)
    if p is None:
        raise ProviderNotFound(provider_id)
    return p


def list_providers(session: Session) -> list[Provider]:
    return list(session.scalars(select(Provider).order_by(Provider.name)))


def update_provider(session: Session, provider_id: str, **fields) -> Provider:
    p = get_provider(session, provider_id)
    for k, v in fields.items():
        setattr(p, k, v)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ProviderNameTaken(fields.get("name")) from exc
    session.refresh(p)
    return p


def _profiles_referencing_endpoint(session: Session, endpoint_id: str) -> bool:
    """True if any profile references ``endpoint_id`` directly or inside
    ``backend_config`` (e.g. a per-agent endpoint reference).

    Per-agent references live inside a JSON blob, so relational integrity is
    enforced procedurally rather than via a foreign key. Profile counts are
    small enough that a Python-side scan is fine and stays portable across
    SQLite/Postgres (vs. a SQLite-only ``json_each`` query).
    """
    direct = session.scalar(
        select(Profile.id).where(Profile.endpoint_id == endpoint_id).limit(1)
    )
    if direct is not None:
        return True
    for profile in session.scalars(select(Profile)):
        cfg = profile.backend_config or {}
        for agent in cfg.get("agents", []) or []:
            if isinstance(agent, dict) and agent.get("endpoint_id") == endpoint_id:
                return True
    return False


def delete_provider(session: Session, provider_id: str) -> None:
    """Delete a provider, cascading its unreferenced endpoints.

    Raises :class:`ProviderInUse` if any of its endpoints is still referenced
    by a profile (directly or via ``backend_config``).
    """
    p = get_provider(session, provider_id)
    for ep in p.endpoints:
        if _profiles_referencing_endpoint(session, ep.id):
            raise ProviderInUse(provider_id)
    session.delete(p)
    session.commit()


# ---------------------------------------------------------------------------
# ProviderEndpoint CRUD
# ---------------------------------------------------------------------------


def create_endpoint(session: Session, *, provider_id: str, **fields) -> ProviderEndpoint:
    _validate_protocol_kind(fields.get("protocol_kind"))
    _validate_credential_source(fields.get("credential_source", "api_key"))
    # Raises ProviderNotFound if the FK target is missing.
    get_provider(session, provider_id)
    ep = ProviderEndpoint(provider_id=provider_id, **fields)
    session.add(ep)
    session.commit()
    session.refresh(ep)
    return ep


def get_endpoint(session: Session, endpoint_id: str) -> ProviderEndpoint:
    ep = session.get(ProviderEndpoint, endpoint_id)
    if ep is None:
        raise EndpointNotFound(endpoint_id)
    return ep


def list_endpoints(
    session: Session, provider_id: Optional[str] = None,
) -> list[ProviderEndpoint]:
    stmt = select(ProviderEndpoint)
    if provider_id is not None:
        stmt = stmt.where(ProviderEndpoint.provider_id == provider_id)
    return list(session.scalars(stmt.order_by(ProviderEndpoint.label)))


def update_endpoint(session: Session, endpoint_id: str, **fields) -> ProviderEndpoint:
    ep = get_endpoint(session, endpoint_id)
    if "protocol_kind" in fields:
        _validate_protocol_kind(fields["protocol_kind"])
    if "credential_source" in fields:
        _validate_credential_source(fields["credential_source"])
    for k, v in fields.items():
        setattr(ep, k, v)
    session.commit()
    session.refresh(ep)
    return ep


def delete_endpoint(session: Session, endpoint_id: str) -> None:
    get_endpoint(session, endpoint_id)
    if _profiles_referencing_endpoint(session, endpoint_id):
        raise EndpointInUse(endpoint_id)
    ep = get_endpoint(session, endpoint_id)
    session.delete(ep)
    session.commit()


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def _decrypt(secret_box: Optional[ProfileSecretBox], token: Optional[str], *, what: str, ep_id: str):
    if not token:
        return None
    if secret_box is None:
        log.warning("endpoint %s has a %s but no secret box is available", ep_id, what)
        return None
    try:
        return secret_box.decrypt(token)
    except ValueError as exc:
        log.warning("endpoint %s %s unreadable: %s", ep_id, what, exc)
        return None


def _resolve_credential(
    ep: ProviderEndpoint, secret_box: Optional[ProfileSecretBox],
) -> Optional[str]:
    """Resolve the plaintext credential per ``credential_source``.

    Never raises: a missing file/env var/decrypt failure logs a warning and
    resolves to ``None`` so the backend can fall back to ambient credentials.
    """
    source = ep.credential_source
    if source == "none":
        return None
    if source == "api_key":
        value = _decrypt(secret_box, ep.credential, what="credential", ep_id=ep.id)
        return value if isinstance(value, str) else None
    if source == "env_var":
        var_name = _decrypt(secret_box, ep.credential, what="credential", ep_id=ep.id)
        if not isinstance(var_name, str) or not var_name:
            return None
        value = os.environ.get(var_name)
        if value is None:
            log.warning("endpoint %s: env var %s is not set", ep.id, var_name)
        return value
    if source in ("oauth_file", "subscription_file"):
        path = _decrypt(secret_box, ep.credential, what="credential", ep_id=ep.id)
        if not isinstance(path, str) or not path:
            return None
        expanded = os.path.expanduser(path)
        try:
            with open(expanded, "r") as fh:
                return fh.read()
        except OSError as exc:
            log.warning("endpoint %s: credential file %s unreadable: %s", ep.id, expanded, exc)
            return None
    log.warning("endpoint %s: unknown credential_source %r", ep.id, source)
    return None


def _resolve_extra(
    ep: ProviderEndpoint, secret_box: Optional[ProfileSecretBox],
) -> dict:
    value = _decrypt(secret_box, ep.extra, what="extra", ep_id=ep.id)
    return value if isinstance(value, dict) else {}


def resolve_endpoint(
    session: Session,
    endpoint_id: Optional[str],
    secret_box: Optional[ProfileSecretBox],
) -> Optional[ResolvedEndpoint]:
    """Load an endpoint row (plus its provider) and resolve its credential.

    Returns ``None`` when ``endpoint_id`` is unset or the row is gone.
    """
    if not endpoint_id:
        return None
    ep = session.get(ProviderEndpoint, endpoint_id)
    if ep is None:
        log.warning("profile references missing endpoint %s", endpoint_id)
        return None
    provider = ep.provider
    return ResolvedEndpoint(
        id=ep.id,
        label=ep.label,
        protocol_kind=ep.protocol_kind,
        base_url=ep.base_url,
        credential=_resolve_credential(ep, secret_box),
        credential_source=ep.credential_source,
        extra=_resolve_extra(ep, secret_box),
        default_model=ep.default_model,
        models=list(ep.models or []),
        provider_id=provider.id if provider is not None else ep.provider_id,
        provider_name=provider.name if provider is not None else "",
        vendor=provider.vendor if provider is not None else "",
        harness_lock=ep.harness_lock,
    )


def resolve_endpoints_for_profile(
    session: Session,
    profile: Profile,
    secret_box: Optional[ProfileSecretBox],
) -> dict[str, ResolvedEndpoint]:
    """Every endpoint a profile's run touches: the primary plus every distinct
    per-agent ``endpoint_id`` found in ``backend_config["agents"]``, keyed by
    endpoint id."""
    ids: list[str] = []
    if profile.endpoint_id:
        ids.append(profile.endpoint_id)
    for agent in (profile.backend_config or {}).get("agents", []) or []:
        if isinstance(agent, dict) and agent.get("endpoint_id"):
            ids.append(agent["endpoint_id"])
    resolved: dict[str, ResolvedEndpoint] = {}
    for eid in dict.fromkeys(ids):  # de-dupe, preserve order
        r = resolve_endpoint(session, eid, secret_box)
        if r is not None:
            resolved[eid] = r
    return resolved


# ---------------------------------------------------------------------------
# Compatibility gate
# ---------------------------------------------------------------------------


def endpoint_compatible(capability, endpoint_or_row) -> bool:
    """``compatible(harness, endpoint) = protocol intersection AND lock match``.

    ``capability`` is a ``BackendCapability``. Reads ``protocol_kinds`` when
    present, falling back to the pre-rename ``provider_kinds`` attribute so
    this works before and after that rename lands.
    """
    protocol_kinds = getattr(capability, "protocol_kinds", None)
    if not protocol_kinds:
        protocol_kinds = getattr(capability, "provider_kinds", frozenset())
    protocol_kind = getattr(endpoint_or_row, "protocol_kind", None)
    if protocol_kind not in protocol_kinds:
        return False
    harness_lock = getattr(endpoint_or_row, "harness_lock", None)
    if harness_lock is None:
        return True
    return harness_lock == getattr(capability, "code", None)
