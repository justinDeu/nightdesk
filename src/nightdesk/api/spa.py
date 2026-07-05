"""Serves the built SPA (frontend/dist) at the app root ``/``, when a build
exists.

No dist directory (or no ``index.html`` inside it) means no routes are
registered at all — the JSON API is completely unaffected.

IMPORTANT — Vite ``base`` requirement: this mounts the build at ``/``, so
the SPA must be built with the default ``base: "/"`` (an absolute root
prefix). Do not build with a relative ``"./"`` base: it would resolve
correctly only for the bare ``/`` route — any client-side route one or more
segments deep (e.g. ``/tickets/<id>``) would resolve assets relative to that
deeper path instead, which 404s.

``build_router`` must be registered LAST in ``create_app`` (after every
``/api``, ``/auth``, ``/healthz``, and ``/static`` router): its
``GET /{path:path}`` catch-all falls back to ``index.html`` for anything
that isn't a real file, and FastAPI matches routes in registration order —
registering it last is what keeps it from shadowing every other route.

``build_app_redirect_router`` is a transition shim: the SPA used to be
mounted at ``/app`` before this module switched it to root. It 308-redirects
every ``/app/*`` path to the equivalent ``/*`` path so old bookmarks/links
keep working. It must be registered BEFORE ``build_router``'s catch-all (or
after it — either is fine as long as it precedes the root catch-all, since
otherwise ``/{path:path}`` would swallow ``/app/...`` requests as a client
route rather than redirecting them).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse


_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
_NO_CACHE = "no-cache"


def default_dist_root() -> Path:
    """``<repo>/frontend/dist`` — the Vite build output, resolved relative to
    this file's location in an editable/repo checkout. Overridden via
    ``create_app(spa_dist_root=...)`` or the ``NIGHTDESK_SPA_DIST`` config
    field for any other install layout."""
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def _safe_join(root: Path, relative: str) -> Optional[Path]:
    """Resolve ``relative`` under ``root``, refusing anything that escapes it
    (``..`` segments, symlink tricks) via a resolved-path prefix check."""
    try:
        candidate = (root / relative).resolve()
    except (OSError, RuntimeError):
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def build_router(dist_root: Path) -> Optional[APIRouter]:
    """Build the root ``/`` SPA router, or ``None`` if there is no build to serve.

    Requires ``dist_root/index.html`` to exist; a missing/empty dist
    directory is treated as "SPA not built yet", not an error.

    Cache policy:

    - Everything under ``dist_root/assets/`` (Vite's default hashed-asset
      layout) is served ``Cache-Control: public, max-age=31536000,
      immutable`` — the filename changes on every content change, so a
      stale cached copy is never served stale.
    - ``index.html`` (and any other top-level file in the build, e.g. a
      favicon) is served ``Cache-Control: no-cache`` so a fresh deploy's
      HTML — and the new hashed asset names it references — is picked up
      on the next navigation without a hard refresh.
    - Any path that doesn't match a real file falls through to
      ``index.html`` so the client-side router owns the URL. Register this
      router LAST (see module docstring) so it never shadows ``/api``,
      ``/auth``, ``/healthz``, or ``/static``.
    """
    dist_root = dist_root.resolve()
    index_path = dist_root / "index.html"
    if not index_path.is_file():
        return None

    assets_dir = dist_root / "assets"
    router = APIRouter(tags=["spa"])

    def _index_response() -> FileResponse:
        return FileResponse(index_path, headers={"Cache-Control": _NO_CACHE})

    @router.get("/assets/{path:path}")
    async def spa_asset(path: str) -> FileResponse:
        target = _safe_join(assets_dir, path)
        if target is None or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target, headers={"Cache-Control": _IMMUTABLE_CACHE})

    @router.get("/")
    async def spa_root() -> FileResponse:
        return _index_response()

    @router.get("/{path:path}")
    async def spa_catch_all(path: str) -> FileResponse:
        # A non-hashed file that happens to live at the build root (favicon,
        # manifest.json, ...) is served as-is; anything else — every
        # client-side route — falls through to index.html.
        target = _safe_join(dist_root, path)
        if target is not None and target.is_file():
            return FileResponse(target, headers={"Cache-Control": _NO_CACHE})
        return _index_response()

    return router


def build_app_redirect_router() -> APIRouter:
    """Transition shim: 308-redirect every ``/app/*`` path to ``/*``.

    The SPA used to be mounted at ``/app``; this keeps old bookmarks and any
    hardcoded ``/app/...`` links working after the switch to root. Remove
    once nothing references ``/app`` anymore.
    """
    router = APIRouter(tags=["spa-legacy-redirect"])

    @router.get("/app")
    async def redirect_app_root() -> RedirectResponse:
        return RedirectResponse(url="/", status_code=308)

    @router.get("/app/{path:path}")
    async def redirect_app_path(path: str) -> RedirectResponse:
        return RedirectResponse(url=f"/{path}", status_code=308)

    return router
