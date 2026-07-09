"""Filesystem path suggestion endpoint.

Used by the ticket workspace editor to autocomplete directory paths.
Read-only, restricted to local directory listing. Returns directory
entries only (files are skipped) with trailing slash.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from nightdesk.api.auth import require_token_cookie_or_bearer


_MAX_SUGGESTIONS = 25


def _suggest_dirs(
    prefix: str, limit: int = _MAX_SUGGESTIONS, *, include_files: bool = False,
) -> list[str]:
    """Return up to ``limit`` paths matching ``prefix``.

    Prefix semantics:
    - If ``prefix`` ends with '/', list children of that directory.
    - Otherwise, list children of the parent directory whose name starts
      with the basename of ``prefix``.
    - Empty prefix is treated as '/'.

    ``include_files`` (default False, preserving the directory-only ticket
    workspace editor) also returns regular files (no trailing slash) — the
    agent composer's ``@``-file mentions need them.

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
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not is_dir and not include_files:
                    continue
                full = os.path.join(base, entry.name) + ("/" if is_dir else "")
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


def build_router(bearer_token: str) -> APIRouter:
    router = APIRouter(tags=["fs"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("/api/v1/fs/suggest", dependencies=[auth])
    async def suggest_api(prefix: str = Query(default=""),
                          limit: int = Query(default=_MAX_SUGGESTIONS, ge=1, le=100),
                          include_files: bool = Query(default=False)):
        return {"prefix": prefix,
                "matches": _suggest_dirs(prefix, limit, include_files=include_files)}

    return router
