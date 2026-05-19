"""Filesystem path suggestion endpoint.

Used by the board sidebar to autocomplete directory paths. Read-only,
restricted to local directory listing. Returns directory entries only
(files are skipped) with trailing slash.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from nightdesk.api.auth import (
    require_bearer,
    require_token_cookie_or_bearer,
)


_MAX_SUGGESTIONS = 25


def _suggest_dirs(prefix: str, limit: int = _MAX_SUGGESTIONS) -> list[str]:
    """Return up to ``limit`` directory paths matching ``prefix``.

    Prefix semantics:
    - If ``prefix`` ends with '/', list children of that directory.
    - Otherwise, list children of the parent directory whose name starts
      with the basename of ``prefix``.
    - Empty prefix is treated as '/'.

    Display is preserved: if the user typed a path starting with ``~`` or
    ``~user``, the returned suggestions are rewritten to use the same
    tilde token instead of the expanded absolute home directory.
    """
    raw = (prefix or "/").strip() or "/"
    expanded = os.path.expanduser(raw)
    if not expanded.startswith("/"):
        return []

    # If user typed a tilde-form, remember the token and the absolute
    # directory it expanded to so we can fold the home prefix back into
    # the displayed suggestions.
    display_home: tuple[str, str] | None = None
    if raw.startswith("~"):
        slash = raw.find("/")
        token = raw if slash == -1 else raw[:slash]
        home_dir = os.path.expanduser(token)
        if home_dir.startswith("/") and home_dir != token:
            display_home = (home_dir.rstrip("/"), token)

    if expanded.endswith("/"):
        base = expanded
        name_prefix = ""
    else:
        base = os.path.dirname(expanded) or "/"
        if not base.endswith("/"):
            base = base + "/"
        name_prefix = os.path.basename(expanded)

    p = Path(base)
    if not p.is_dir():
        return []

    out: list[str] = []
    try:
        with os.scandir(p) as it:
            for entry in it:
                if entry.name.startswith(".") and not name_prefix.startswith("."):
                    continue
                if name_prefix and not entry.name.startswith(name_prefix):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                full = os.path.join(base, entry.name) + "/"
                if display_home is not None:
                    home_dir, token = display_home
                    if full == home_dir + "/":
                        full = token + "/"
                    elif full.startswith(home_dir + "/"):
                        full = token + full[len(home_dir):]
                out.append(full)
                if len(out) >= limit:
                    break
    except PermissionError:
        return []
    out.sort()
    return out


def build_router(get_session, bearer_token: str, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(tags=["fs"])
    cookie_or_bearer = Depends(require_token_cookie_or_bearer(bearer_token))
    bearer = Depends(require_bearer(bearer_token))

    @router.get("/api/v1/fs/suggest", dependencies=[bearer])
    async def suggest_api(prefix: str = Query(default=""),
                          limit: int = Query(default=_MAX_SUGGESTIONS, ge=1, le=100)):
        return {"prefix": prefix, "matches": _suggest_dirs(prefix, limit)}

    @router.get("/fs/suggest", response_class=HTMLResponse, dependencies=[cookie_or_bearer])
    async def suggest_html(request: Request,
                           q: str = Query(default=""),
                           target: str = Query(default="")):
        """HTML partial for HTMX-driven autocomplete dropdowns.

        ``q`` is the current input value. ``target`` identifies which
        sidebar field the suggestions belong to so multiple inputs on
        the page can each get their own dropdown.
        """
        matches = _suggest_dirs(q) if q else []
        return templates.TemplateResponse(request, "partials/path_suggest.html", {
            "matches": matches,
            "target": target,
            "q": q,
        })

    return router
