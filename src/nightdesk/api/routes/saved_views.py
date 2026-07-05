"""Saved views: JSON API.

  GET    /api/v1/views           — list views with composed URLs
  POST   /api/v1/views           — create
  PATCH  /api/v1/views/{id}      — rename
  DELETE /api/v1/views/{id}      — delete
  POST   /api/v1/views/reorder   — reorder
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.api.schemas import SavedViewCreate, SavedViewOut, SavedViewReorder, SavedViewUpdate
from nightdesk.domain.saved_views import (
    SavedViewNameTaken,
    SavedViewNotFound,
    create_saved_view,
    delete_saved_view,
    list_saved_views,
    rename_saved_view,
    reorder_saved_views,
    view_url,
)


def _view_out(v) -> SavedViewOut:
    return SavedViewOut(
        id=v.id, name=v.name, surface=v.surface, params=v.params, url=view_url(v),
    )


def build_api_router(get_session, bearer_token: str) -> APIRouter:
    """JSON /api/v1/views — CRUD + reorder over saved views."""
    router = APIRouter(prefix="/api/v1", tags=["saved_views_api"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("/views", response_model=list[SavedViewOut], dependencies=[auth])
    async def list_views(session: Session = Depends(get_session)):
        return [_view_out(v) for v in list_saved_views(session)]

    @router.post(
        "/views", response_model=SavedViewOut, status_code=201, dependencies=[auth],
    )
    async def create_view(
        payload: SavedViewCreate, session: Session = Depends(get_session),
    ):
        try:
            view = create_saved_view(
                session, name=payload.name, surface=payload.surface,
                params=payload.params,
            )
        except SavedViewNameTaken:
            raise HTTPException(409, f"a view named {payload.name!r} already exists")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return _view_out(view)

    @router.patch(
        "/views/{view_id}", response_model=SavedViewOut, dependencies=[auth],
    )
    async def update_view(
        view_id: str, payload: SavedViewUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            view = rename_saved_view(session, view_id, name=payload.name)
        except SavedViewNotFound:
            raise HTTPException(404, "not found")
        except SavedViewNameTaken:
            raise HTTPException(409, f"a view named {payload.name!r} already exists")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return _view_out(view)

    @router.delete("/views/{view_id}", status_code=204, dependencies=[auth])
    async def delete_view_api(view_id: str, session: Session = Depends(get_session)):
        try:
            delete_saved_view(session, view_id)
        except SavedViewNotFound:
            raise HTTPException(404, "not found")
        return None

    @router.post(
        "/views/reorder", response_model=list[SavedViewOut], dependencies=[auth],
    )
    async def reorder_views(
        payload: SavedViewReorder, session: Session = Depends(get_session),
    ):
        reorder_saved_views(session, payload.view_ids)
        return [_view_out(v) for v in list_saved_views(session)]

    return router
