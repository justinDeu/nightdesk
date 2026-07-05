"""Browser login flow: handshake (one-shot) + manual login fallback.

The ``nightdesk setup`` command mints a short-lived one-shot token and
opens ``/auth/handshake?token=...`` in the user's browser. The handshake
endpoint validates and consumes the token, sets the signed session cookie,
and redirects to ``/`` (the SPA root). No copy-pasting bearer tokens.

The SPA owns the login UI at its own client-side ``/login`` route (see
``frontend/src/routes/login.tsx``); this module renders no HTML itself —
every failure path here is either a JSON error (``POST /auth/login``, which
the SPA's fetch call inspects via `res.ok`) or a redirect to ``/login``
(``GET /auth/login``, ``GET /auth/handshake`` on an expired/invalid token),
never a server-rendered page.

The one-shot store is in-memory (a process-local dict). Restarting the
API invalidates outstanding handshakes; ``nightdesk login`` regenerates
one on demand. Token TTL is short (60s) so a leaked URL becomes useless
quickly.
"""
from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, Form, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from nightdesk.api.auth import SESSION_COOKIE, issue_session_cookie, require_bearer


_ONE_SHOT_TTL_SECONDS = 60


class OneShotStore:
    """In-memory single-use tokens with a short TTL."""

    def __init__(self) -> None:
        self._tokens: dict[str, float] = {}

    def mint(self) -> str:
        self._sweep()
        token = "nds_" + secrets.token_urlsafe(24)
        self._tokens[token] = time.monotonic() + _ONE_SHOT_TTL_SECONDS
        return token

    def consume(self, token: str) -> bool:
        self._sweep()
        expires = self._tokens.pop(token, None)
        if expires is None:
            return False
        return expires > time.monotonic()

    def _sweep(self) -> None:
        now = time.monotonic()
        for tok, exp in list(self._tokens.items()):
            if exp <= now:
                self._tokens.pop(tok, None)


def build_router(
    *,
    bearer_token: str,
    one_shot_store: OneShotStore,
    session_cookie_max_age: int,
    bind_host: str = "127.0.0.1",
    bind_port: int = 8765,
) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])
    _admin_dep = Depends(require_bearer(bearer_token))

    def _set_cookie(response: Response) -> None:
        response.set_cookie(
            key=SESSION_COOKIE,
            value=issue_session_cookie(bearer_token),
            max_age=session_cookie_max_age,
            httponly=True,
            samesite="strict",
            path="/",
        )

    @router.post("/mint-handshake", response_class=JSONResponse)
    async def mint_handshake(_admin=_admin_dep):
        """Mint a one-shot handshake token (requires bearer auth).

        Returns ``{"token": "...", "url": "http://.../auth/handshake?token=..."}``.
        The ``nightdesk setup`` and ``nightdesk login`` commands call this to
        obtain a browser-openable URL without copy-pasting the bearer token.
        """
        token = one_shot_store.mint()
        url = f"http://{bind_host}:{bind_port}/auth/handshake?token={token}"
        return {"token": token, "url": url}

    @router.get("/handshake")
    async def handshake(token: str):
        if not one_shot_store.consume(token):
            # Expired or already-used token: send them to the SPA's login
            # form to re-authenticate with the bearer token instead.
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        _set_cookie(response)
        return response

    @router.get("/login")
    async def login_redirect():
        """No server-rendered login page — the SPA owns ``/login``.

        Kept as a redirect (rather than removed) because ``nightdesk setup``
        prints this URL as a manual fallback when the browser can't be
        auto-opened; the printed URL must keep working.
        """
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    @router.post("/login")
    async def login_submit(bearer: str = Form(...)):
        if not bearer_token or bearer.strip() != bearer_token:
            return JSONResponse(
                {"error": "Bearer token does not match."},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        _set_cookie(response)
        return response

    @router.post("/logout")
    async def logout():
        response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router
