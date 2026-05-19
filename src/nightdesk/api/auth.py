"""Auth primitives for the nightdesk API.

Two kinds of principal:

- ``AdminPrincipal``: holds the long-lived bearer token from
  ``~/.config/nightdesk/config.toml``. Set via Authorization header for
  API clients, via a signed cookie for the browser UI.
- ``RunPrincipal``: a per-run scoped token issued by the worker just
  before a sandboxed CC is launched. Carries a small allowlist of
  scopes (see ``nightdesk.domain.run_tokens``). Sandboxed runs that
  call back into the API authenticate with one of these so a misbehaving
  agent can only touch its own ticket.

Auth deps in this module:

- ``require_admin(bearer)``: classic bearer-only check, for routes that
  shouldn't accept a run-token principal.
- ``require_principal(bearer)``: resolve either kind; useful for routes
  shared between admin clients and runs.
- ``require_scope(bearer, *scopes)``: admin passes; run principal must
  hold every named scope. For ``*.self`` scopes the route is expected
  to validate the ticket id against ``principal.scope_data['ticket_id']``.
- ``require_cookie_or_bearer(bearer)``: UI auth — accepts the signed
  session cookie or a Bearer header.

The signed cookie is an itsdangerous URLSafeTimedSerializer payload
containing ``{"bearer": "..."}``; the serializer's secret key is the
bearer token itself. Rotating the bearer invalidates all existing cookies.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional, Union

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from nightdesk.api.deps import get_session_dep
from nightdesk.domain.run_tokens import RunPrincipal, resolve_run_token


log = logging.getLogger(__name__)


SESSION_COOKIE = "nightdesk_session"
_SERIALIZER_SALT = "nightdesk.session.v1"


@dataclass
class AdminPrincipal:
    """Holder of the long-lived bearer token."""
    kind: str = "admin"


Principal = Union[AdminPrincipal, RunPrincipal]


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


# ---------------------------------------------------------------------------
# Legacy helpers — kept callable for routes that haven't migrated yet.
# ---------------------------------------------------------------------------


def require_bearer(token_value: str):
    """Strict admin-bearer check. Run tokens are rejected."""
    async def _dep(request: Request):
        provided = _extract_bearer(request) or ""
        if not token_value or provided != token_value:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    return _dep


def require_token_cookie_or_bearer(
    token_value: str,
    cookie_name: str = "nightdesk_token",
    session_cookie_max_age: int = 60 * 60 * 24 * 30,
):
    """UI auth: accepts a signed session cookie, a legacy cookie, or a Bearer.

    ``cookie_name`` is the legacy literal-bearer cookie kept for backwards
    compat with anything still setting it directly. New flows should set
    ``nightdesk_session`` via ``issue_session_cookie``.
    """
    async def _dep(request: Request):
        # Signed session cookie (new path).
        signed = request.cookies.get(SESSION_COOKIE)
        if signed and verify_session_cookie(token_value, signed, session_cookie_max_age):
            return
        # Bearer header.
        header_token = _extract_bearer(request) or ""
        if token_value and header_token == token_value:
            return
        # Legacy literal-bearer cookie.
        legacy = request.cookies.get(cookie_name) or ""
        if token_value and legacy == token_value:
            return
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth required")
    return _dep


# ---------------------------------------------------------------------------
# Principal resolution.
# ---------------------------------------------------------------------------


def require_principal(token_value: str, engine):
    """Resolve to AdminPrincipal or RunPrincipal; 401 if neither."""
    get_session = get_session_dep(engine)

    async def _dep(request: Request, session: Session = Depends(get_session)) -> Principal:
        provided = _extract_bearer(request) or ""
        if token_value and provided == token_value:
            return AdminPrincipal()
        if provided.startswith("ndr_"):
            principal = resolve_run_token(session, provided)
            if principal is not None:
                return principal
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    return _dep


def require_admin(token_value: str, engine):
    """Like require_principal but rejects RunPrincipal."""
    inner = require_principal(token_value, engine)

    async def _dep(
        request: Request,
        principal: Principal = Depends(inner),
    ) -> AdminPrincipal:
        if not isinstance(principal, AdminPrincipal):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="admin token required for this route")
        return principal

    return _dep


def require_scopes(token_value: str, engine, scopes: Iterable[str]):
    """Allow admin OR a run principal that holds ALL named scopes."""
    needed = tuple(scopes)
    inner = require_principal(token_value, engine)

    async def _dep(
        request: Request,
        principal: Principal = Depends(inner),
    ) -> Principal:
        if isinstance(principal, AdminPrincipal):
            return principal
        missing = [s for s in needed if s not in principal.scopes]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing scopes: {missing}",
            )
        return principal

    return _dep


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
