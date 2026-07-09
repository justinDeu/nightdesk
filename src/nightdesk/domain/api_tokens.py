"""Durable, scoped API tokens (``ndk_``) and the unified token resolver.

Generalizes ``domain/run_tokens.py``: a cleartext token is ``"ndk_" +
token_urlsafe(32)``, shown once at mint; only its sha256 hash is persisted.
Lookup by sha256 is inherently constant-time-safe for 256-bit random tokens —
no KDF needed (see the doc §11). Revocation is a row flag checked at resolution.

``resolve_token`` is the single resolver for every ``ndk_``/``ndr_`` credential:
it checks ``api_tokens`` first, then falls back to the legacy ``run_tokens``
table (Phase 1 keeps run tokens in their own table). Both produce a
``TokenPrincipal`` with a unified scope vocabulary.

See ``docs/design/agent-token-permissions.md`` §3, §5, §7.
"""
from __future__ import annotations

import hashlib
import secrets as secrets_mod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import ApiToken, RunToken
from nightdesk.domain import scopes as scope_vocab


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite drops tzinfo; assume stored timestamps are UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@dataclass
class TokenPrincipal:
    """Resolved identity for a request authenticated by any scoped token.

    Absorbs the old ``RunPrincipal`` (same ``scopes``/``scope_data`` plus a
    stable ``id``, ``name``, and ``kind``). ``run_id``/``ticket_id`` are set only
    for run-kind tokens; ``enforce_self_ticket`` reads ``scope_data['ticket_id']``.
    """
    kind: str
    scopes: tuple[str, ...]
    scope_data: dict
    id: Optional[str] = None
    name: Optional[str] = None
    token_hash: Optional[str] = None
    run_id: Optional[str] = None
    ticket_id: Optional[str] = None


class HumanOnlyScopeError(ValueError):
    """Raised when a mint request names a human-only (unmintable) scope."""

    def __init__(self, offending: list[str]):
        self.offending = offending
        super().__init__(f"human-only scopes may not be minted: {offending}")


class UnknownScopeError(ValueError):
    def __init__(self, offending: list[str]):
        self.offending = offending
        super().__init__(f"unknown scopes: {offending}")


@dataclass
class IssuedApiToken:
    cleartext: str
    id: str
    prefix_hint: str
    name: str
    scopes: list[str]
    bundle: Optional[str]
    expires_at: Optional[datetime]


def mint_api_token(
    session: Session,
    *,
    name: str,
    scopes: Iterable[str],
    bundle: Optional[str] = None,
    profile_allowlist: Optional[list[str]] = None,
    project_allowlist: Optional[list[str]] = None,
    expires_at: Optional[datetime] = None,
    created_by: str = "admin",
) -> IssuedApiToken:
    """Create, persist, and return a fresh durable ``ndk_`` token.

    Rejects unknown scopes and any human-only scope outright — no token may ever
    hold a human-only scope (§4.3). The cleartext is returned once; the caller
    reveals it to the human and then it is unrecoverable.
    """
    requested = list(dict.fromkeys(scopes))  # de-dupe, preserve order
    unknown = scope_vocab.unknown_scopes(requested)
    if unknown:
        raise UnknownScopeError(unknown)
    human = scope_vocab.human_only(requested)
    if human:
        raise HumanOnlyScopeError(human)

    cleartext = "ndk_" + secrets_mod.token_urlsafe(32)
    token_hash = _hash(cleartext)
    scope_data: dict = {}
    if profile_allowlist:
        scope_data["profile_allowlist"] = list(profile_allowlist)
    if project_allowlist:
        scope_data["project_allowlist"] = list(project_allowlist)

    row = ApiToken(
        token_hash=token_hash,
        prefix_hint=cleartext[:12],
        name=name,
        kind="agent",
        scopes=requested,
        bundle=bundle,
        scope_data=scope_data,
        created_at=datetime.now(timezone.utc),
        expires_at=expires_at,
        created_by=created_by,
    )
    session.add(row)
    session.commit()
    return IssuedApiToken(
        cleartext=cleartext,
        id=row.id,
        prefix_hint=row.prefix_hint,
        name=row.name,
        scopes=list(requested),
        bundle=bundle,
        expires_at=_aware(row.expires_at),
    )


def list_api_tokens(session: Session, *, include_revoked: bool = True) -> list[ApiToken]:
    stmt = select(ApiToken).where(ApiToken.kind == "agent")
    if not include_revoked:
        stmt = stmt.where(ApiToken.revoked_at.is_(None))
    stmt = stmt.order_by(ApiToken.created_at.desc())
    return list(session.execute(stmt).scalars())


def revoke_api_token(session: Session, token_id: str) -> bool:
    row = session.get(ApiToken, token_id)
    if row is None:
        return False
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        session.commit()
    return True


def delete_api_token(session: Session, token_id: str) -> bool:
    row = session.get(ApiToken, token_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# Update last_used_at at most once per minute to keep hot paths cheap.
_LAST_USED_RESOLUTION_SECONDS = 60


def _touch_last_used(session: Session, row: ApiToken) -> None:
    now = datetime.now(timezone.utc)
    prev = _aware(row.last_used_at)
    if prev is not None and (now - prev).total_seconds() < _LAST_USED_RESOLUTION_SECONDS:
        return
    row.last_used_at = now
    session.commit()


def _principal_from_api_token(session: Session, row: ApiToken) -> Optional[TokenPrincipal]:
    if row.revoked_at is not None:
        return None
    expires_at = _aware(row.expires_at)
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        return None
    _touch_last_used(session, row)
    return TokenPrincipal(
        kind=row.kind,
        scopes=tuple(row.scopes or ()),
        scope_data=dict(row.scope_data or {}),
        id=row.id,
        name=row.name,
        token_hash=row.token_hash,
        run_id=row.run_id,
        ticket_id=row.ticket_id,
    )


def _principal_from_run_token(row: RunToken) -> Optional[TokenPrincipal]:
    if row.revoked_at is not None:
        return None
    expires_at = _aware(row.expires_at)
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        return None
    return TokenPrincipal(
        kind="run",
        scopes=scope_vocab.expand_run_scopes(row.scopes or ()),
        scope_data=dict(row.scope_data or {}),
        id=None,
        name=None,
        token_hash=row.token_hash,
        run_id=row.run_id,
        ticket_id=row.ticket_id,
    )


def resolve_token(session: Session, cleartext: str) -> Optional[TokenPrincipal]:
    """Resolve any ``ndk_``/``ndr_`` cleartext to a principal, or None.

    Prefix-agnostic: the sha256 hash finds the row regardless of prefix (the
    prefix is cosmetic, a lifecycle signal for humans reading logs). Checks
    ``api_tokens`` first, then the legacy ``run_tokens`` table.
    """
    if not cleartext or not (cleartext.startswith("ndk_") or cleartext.startswith("ndr_")):
        return None
    h = _hash(cleartext)
    api_row = session.execute(
        select(ApiToken).where(ApiToken.token_hash == h)
    ).scalar_one_or_none()
    if api_row is not None:
        return _principal_from_api_token(session, api_row)
    run_row = session.get(RunToken, h)
    if run_row is not None:
        return _principal_from_run_token(run_row)
    return None
