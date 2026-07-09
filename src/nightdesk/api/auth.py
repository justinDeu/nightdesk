"""Auth primitives for the nightdesk API.

Two kinds of principal:

- ``AdminPrincipal``: the root bearer from ``~/.config/nightdesk/config.toml``
  (Authorization header) or a signed session cookie (browser UI). Omnipotent —
  scope checks are bypassed. The cookie is a human-browser artifact: an
  ``ndk_``/``ndr_`` token can never obtain one (``/auth/login`` +
  ``/auth/mint-handshake`` accept the root bearer only).
- ``TokenPrincipal``: any scoped ``ndk_`` (durable, agent) or ``ndr_``
  (ephemeral, per-run) token. Carries an expanded ``scopes`` snapshot and
  ``scope_data`` restrictions. Resolved by ``domain.api_tokens.resolve_token``
  (one resolver over both token tables).

Enforcement:

- ``make_scoped(bearer, engine)`` builds the ``scoped(*needed)`` dependency
  factory used by every router. Resolution order: valid cookie -> admin; bearer
  == root -> admin; ``ndk_``/``ndr_`` -> hash lookup + ``needed ⊆ scopes``;
  else 401. A token missing a scope 403s with a body naming ``missing_scopes``.
- ``require_scopes`` is a thin wrapper over the same resolver, kept for the
  run-token write-back routes in ``runs.py``.
- ``require_bearer`` is the strict root-only gate for ``/auth/*`` and the
  token mint/revoke routes.
- ``enforce_self_ticket`` constrains ``*.self`` run-token scopes to their ticket.

The signed cookie is an itsdangerous payload ``{"bearer": "..."}`` whose secret
key is the bearer itself; rotating the bearer invalidates all cookies.
"""
from __future__ import annotations

import logging
import secrets as secrets_mod
from dataclasses import dataclass
from typing import Iterable, Optional, Union

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from nightdesk.api.deps import get_session_dep
from nightdesk.domain.api_tokens import TokenPrincipal, resolve_token
from nightdesk.domain.scopes import HUMAN_ONLY_SCOPES


log = logging.getLogger(__name__)


SESSION_COOKIE = "nightdesk_session"
_SERIALIZER_SALT = "nightdesk.session.v1"


@dataclass
class AdminPrincipal:
    """Holder of the long-lived root bearer (header or signed cookie)."""
    kind: str = "admin"


Principal = Union[AdminPrincipal, TokenPrincipal]


def _session_serializer(bearer_token: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(bearer_token, salt=_SERIALIZER_SALT)


def issue_session_cookie(bearer_token: str) -> str:
    """Sign a session payload tied to the current bearer."""
    return _session_serializer(bearer_token).dumps({"bearer": bearer_token})


def verify_session_cookie(bearer_token: str, raw: str, max_age: int) -> bool:
    if not raw or not bearer_token:
        return False
    try:
        payload = _session_serializer(bearer_token).loads(raw, max_age=max_age)
    except SignatureExpired:
        return False
    except BadSignature:
        return False
    return isinstance(payload, dict) and payload.get("bearer") == bearer_token


def _extract_bearer(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header.removeprefix("Bearer ").strip()
    return None


def _bearer_matches(provided: str, token_value: str) -> bool:
    """Constant-time compare of the presented bearer against the root token."""
    if not token_value or not provided:
        return False
    return secrets_mod.compare_digest(provided, token_value)


# ---------------------------------------------------------------------------
# Root-bearer-only gate (auth routes + token mint/revoke).
# ---------------------------------------------------------------------------


def require_bearer(token_value: str):
    """Strict root-bearer check. Scoped tokens and cookies are rejected."""
    async def _dep(request: Request):
        provided = _extract_bearer(request) or ""
        if not _bearer_matches(provided, token_value):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    return _dep


def require_token_cookie_or_bearer(
    token_value: str,
    cookie_name: str = "nightdesk_token",
    session_cookie_max_age: int = 60 * 60 * 24 * 30,
):
    """UI auth: signed session cookie, a legacy literal cookie, or a root Bearer.

    Kept for any router not yet migrated to ``scoped()``; a scoped ``ndk_`` token
    fails the literal-bearer compare here and 401s (the doc's incremental
    default-deny: un-migrated routes are inaccessible to agent tokens, never
    over-accessible).
    """
    async def _dep(request: Request):
        signed = request.cookies.get(SESSION_COOKIE)
        if signed and verify_session_cookie(token_value, signed, session_cookie_max_age):
            return
        header_token = _extract_bearer(request) or ""
        if _bearer_matches(header_token, token_value):
            return
        legacy = request.cookies.get(cookie_name) or ""
        if _bearer_matches(legacy, token_value):
            return
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth required")
    return _dep


# ---------------------------------------------------------------------------
# Unified principal resolution.
# ---------------------------------------------------------------------------


_LEGACY_TOKEN_COOKIE = "nightdesk_token"


def _resolve_principal(
    request: Request,
    session: Session,
    token_value: str,
    session_cookie_max_age: int,
) -> Optional[Principal]:
    """AdminPrincipal (cookie/root bearer) or TokenPrincipal (ndk_/ndr_), or None."""
    signed = request.cookies.get(SESSION_COOKIE)
    if signed and verify_session_cookie(token_value, signed, session_cookie_max_age):
        return AdminPrincipal()
    provided = _extract_bearer(request) or ""
    if _bearer_matches(provided, token_value):
        return AdminPrincipal()
    # Legacy literal-bearer cookie (pre-signed-session). Still admin; kept for
    # the browser flow and existing clients that set it directly.
    legacy = request.cookies.get(_LEGACY_TOKEN_COOKIE) or ""
    if _bearer_matches(legacy, token_value):
        return AdminPrincipal()
    if provided.startswith("ndk_") or provided.startswith("ndr_"):
        principal = resolve_token(session, provided)
        if principal is not None:
            return principal
    return None


def make_scoped(
    token_value: str,
    engine,
    *,
    session_cookie_max_age: int = 60 * 60 * 24 * 30,
):
    """Build the ``scoped(*needed)`` dependency factory for a router.

    Constructed once in ``app.py`` and threaded into every ``build_router``.
    ``scoped(*needed)`` returns a FastAPI dependency that resolves the principal,
    passes admins straight through, and otherwise requires the token to hold
    every scope in ``needed`` (403 naming the missing scopes if not).
    """
    get_session = get_session_dep(engine)

    def scoped(*needed: str):
        required = tuple(needed)

        async def _dep(
            request: Request,
            session: Session = Depends(get_session),
        ) -> Principal:
            principal = _resolve_principal(
                request, session, token_value, session_cookie_max_age
            )
            if principal is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
                )
            if isinstance(principal, AdminPrincipal):
                return principal
            missing = [s for s in required if s not in principal.scopes]
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=_missing_scope_detail(missing, principal),
                )
            return principal

        return _dep

    return scoped


def _missing_scope_detail(missing: list[str], principal: Principal) -> dict:
    """The self-diagnosis 403 body (§5.3)."""
    name = getattr(principal, "name", None) or getattr(principal, "kind", "token")
    human = [s for s in missing if s in HUMAN_ONLY_SCOPES]
    if human:
        hint = (
            f"'{human[0]}' is a human-only action; it requires the admin session "
            f"and can never be granted to a token."
        )
    else:
        hint = (
            f"This token lacks '{missing[0]}'. An operator can grant it in "
            f"Settings -> Access tokens."
        )
    return {
        "detail": "missing scope",
        "missing_scopes": missing,
        "token": name,
        "hint": hint,
    }


def require_scopes(token_value: str, engine, scopes: Iterable[str]):
    """Admin OR a token holding ALL named scopes. Thin wrapper over the resolver.

    Retained for the run-token write-back routes in ``runs.py``; new routers use
    ``make_scoped`` / ``scoped`` directly.
    """
    scoped = make_scoped(token_value, engine)
    return scoped(*scopes)


def enforce_profile_allowlist(principal: Principal, profile_id: Optional[str]) -> None:
    """Constrain ``tickets.create`` to a token's ``profile_allowlist`` (§4.4).

    Admins are unconstrained. A token with a ``profile_allowlist`` in its
    ``scope_data`` may only create tickets bound to a listed profile — the
    allowlist IS the blast-radius knob for what a created ticket can run as.
    Generalizes the run token's ``create_profile_allowlist``.
    """
    if isinstance(principal, AdminPrincipal):
        return
    allow = principal.scope_data.get("profile_allowlist") or principal.scope_data.get(
        "create_profile_allowlist"
    )
    if not allow:
        return
    if profile_id not in allow:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "detail": "profile not allowed",
                "token": getattr(principal, "name", None) or getattr(principal, "kind", "token"),
                "hint": (
                    "This token's profile_allowlist does not include the requested "
                    "profile; it can only create tickets on allowlisted profiles."
                ),
            },
        )


def enforce_self_ticket(principal: Principal, ticket_id: str) -> None:
    """For ``*.self`` scopes the principal's ticket must match the route's."""
    if isinstance(principal, AdminPrincipal):
        return
    own = principal.scope_data.get("ticket_id")
    if own != ticket_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="run token only authorizes operations on its own ticket",
        )
