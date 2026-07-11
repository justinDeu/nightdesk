"""Access-token mint / list / revoke API — admin session only.

Every route here gates on ``scoped(TOKENS_ADMIN)``: the admin principal (the
root config bearer OR the browser's signed session cookie — the Settings UI
speaks cookie auth) passes, while ``tokens.admin`` is human-only and unmintable
(§4.3), so no ``ndk_``/``ndr_`` token can ever hold it — a scoped token 403s
with the human-only hint, and anything else 401s. A token that could mint
tokens would be admin. The mint response is the ONLY place a full token value
is ever returned; the list view carries metadata + ``prefix_hint`` and never a
value or hash. See ``docs/design/agent-token-permissions.md`` §3, §6.3.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from nightdesk.api.schemas import (
    TokenMintRequest,
    TokenMintResult,
    TokenOut,
)
from nightdesk.domain import scopes as scope_vocab
from nightdesk.domain.api_tokens import (
    HumanOnlyScopeError,
    UnknownScopeError,
    delete_api_token,
    list_api_tokens,
    mint_api_token,
    revoke_api_token,
)


def _token_out(row) -> TokenOut:
    return TokenOut(
        id=row.id,
        name=row.name,
        kind=row.kind,
        bundle=row.bundle,
        scopes=list(row.scopes or []),
        scope_data=dict(row.scope_data or {}),
        prefix_hint=row.prefix_hint,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
    )


def build_router(get_session, bearer_token: str, scoped) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/tokens",
        tags=["tokens"],
        # tokens.admin is human-only + unmintable: only AdminPrincipal (root
        # bearer or signed session cookie) can ever satisfy this.
        dependencies=[Depends(scoped(scope_vocab.TOKENS_ADMIN))],
    )

    @router.get("/catalog")
    async def catalog():
        """Scope vocabulary + bundles for the mint UI.

        ``human_only`` scopes are returned so the client can render them
        disabled (teach the model) rather than hide them.
        """
        return {
            "scopes": list(scope_vocab.ALL_SCOPES),
            "human_only": sorted(scope_vocab.HUMAN_ONLY_SCOPES),
            "bundles": scope_vocab.BUNDLES,
        }

    @router.get("", response_model=list[TokenOut])
    async def list_tokens(session: Session = Depends(get_session)):
        return [_token_out(r) for r in list_api_tokens(session)]

    @router.post("", response_model=TokenMintResult, status_code=status.HTTP_201_CREATED)
    async def mint(payload: TokenMintRequest, session: Session = Depends(get_session)):
        # Resolve scopes: explicit list wins; else expand the bundle.
        scopes = payload.scopes
        if scopes is None:
            if not payload.bundle:
                raise HTTPException(422, "provide either a bundle or an explicit scopes list")
            try:
                scopes = scope_vocab.bundle_scopes(payload.bundle)
            except KeyError:
                raise HTTPException(422, f"unknown bundle: {payload.bundle!r}")
        if not scopes:
            raise HTTPException(422, "a token must have at least one scope")

        expires_at: Optional[datetime] = None
        if payload.expires_in_days is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)

        try:
            issued = mint_api_token(
                session,
                name=payload.name,
                scopes=scopes,
                bundle=payload.bundle,
                profile_allowlist=payload.profile_allowlist,
                project_allowlist=payload.project_allowlist,
                expires_at=expires_at,
            )
        except HumanOnlyScopeError as e:
            raise HTTPException(
                status_code=422,
                detail={
                    "detail": "human-only scopes may not be minted",
                    "human_only_scopes": e.offending,
                    "hint": "These actions require the admin session and can never be granted to a token.",
                },
            )
        except UnknownScopeError as e:
            raise HTTPException(422, f"unknown scopes: {e.offending}")

        # Re-read the row for the full metadata view (created_at, prefix_hint...).
        from nightdesk.db.models import ApiToken
        row = session.get(ApiToken, issued.id)
        out = _token_out(row)
        return TokenMintResult(**out.model_dump(), token=issued.cleartext)

    @router.post("/{token_id}/revoke", response_model=TokenOut)
    async def revoke(token_id: str, session: Session = Depends(get_session)):
        if not revoke_api_token(session, token_id):
            raise HTTPException(404, "not found")
        from nightdesk.db.models import ApiToken
        return _token_out(session.get(ApiToken, token_id))

    @router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete(token_id: str, session: Session = Depends(get_session)):
        if not delete_api_token(session, token_id):
            raise HTTPException(404, "not found")
        return None

    return router
