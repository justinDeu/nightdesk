"""Saved views: HTMX routes + JSON API.

HTMX routes (cookie-or-bearer auth):
  GET  /views/menu             — nav popover fragment listing all views
  POST /views                  — save a new view (JSON body)
  POST /views/{id}/delete      — delete and return refreshed menu
  POST /views/{id}/rename      — rename (form: name=) and return refreshed menu

JSON API (same auth):
  GET  /api/v1/views           — list views with composed URLs
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.domain.saved_views import (
    SavedViewNameTaken,
    SavedViewNotFound,
    create_saved_view,
    delete_saved_view,
    list_saved_views,
    rename_saved_view,
    view_url,
)


def build_router(
    get_session,
    bearer_token: str,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter(tags=["saved_views"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    def _menu_response(request: Request, session: Session) -> HTMLResponse:
        views = list_saved_views(session)
        return templates.TemplateResponse(
            request,
            "partials/saved_view_menu.html",
            {"views": views},
        )

    @router.get("/views/menu", response_class=HTMLResponse, dependencies=[auth])
    async def views_menu(
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Nav popover fragment: list of saved view links with delete buttons."""
        return _menu_response(request, session)

    @router.post("/views", dependencies=[auth])
    async def save_view(
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Create a new saved view from a JSON body {name, surface, params}."""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "JSON body required")
        name = (body.get("name") or "").strip()
        surface = (body.get("surface") or "").strip()
        params = body.get("params") or {}
        try:
            view = create_saved_view(session, name=name, surface=surface, params=params)
        except SavedViewNameTaken:
            raise HTTPException(409, f"a view named {name!r} already exists")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return JSONResponse(
            {
                "id": view.id,
                "name": view.name,
                "surface": view.surface,
                "params": view.params,
                "url": view_url(view),
            },
            status_code=201,
        )

    @router.post(
        "/views/{view_id}/delete",
        response_class=HTMLResponse,
        dependencies=[auth],
    )
    async def delete_view(
        request: Request,
        view_id: str,
        session: Session = Depends(get_session),
    ):
        """Delete a saved view and return the refreshed nav menu fragment."""
        try:
            delete_saved_view(session, view_id)
        except SavedViewNotFound:
            raise HTTPException(404, "not found")
        return _menu_response(request, session)

    @router.post(
        "/views/{view_id}/rename",
        response_class=HTMLResponse,
        dependencies=[auth],
    )
    async def rename_view(
        request: Request,
        view_id: str,
        name: str = Form(...),
        session: Session = Depends(get_session),
    ):
        """Rename a saved view and return the refreshed nav menu fragment."""
        try:
            rename_saved_view(session, view_id, name=name)
        except SavedViewNotFound:
            raise HTTPException(404, "not found")
        except SavedViewNameTaken:
            raise HTTPException(409, f"a view named {name!r} already exists")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return _menu_response(request, session)

    return router


def build_api_router(get_session, bearer_token: str) -> APIRouter:
    """JSON /api/v1/views — list all saved views with composed navigation URLs."""
    router = APIRouter(prefix="/api/v1", tags=["saved_views_api"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("/views", dependencies=[auth])
    async def list_views(session: Session = Depends(get_session)):
        views = list_saved_views(session)
        return [
            {
                "id": v.id,
                "name": v.name,
                "surface": v.surface,
                "params": v.params,
                "url": view_url(v),
            }
            for v in views
        ]

    return router
