from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_bearer
from nightdesk.api.schemas import ProjectCreate, ProjectOut, ProjectUpdate
from nightdesk.domain.projects import (
    ProjectNameTaken,
    ProjectNotFound,
    archive_project,
    create_project,
    get_project,
    list_projects,
    update_project,
)


def _coerce_workspaces(workspaces):
    if workspaces is None:
        return None
    return [w.model_dump() if hasattr(w, "model_dump") else dict(w) for w in workspaces]


def build_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects",
        tags=["projects"],
        dependencies=[Depends(require_bearer(bearer_token))],
    )

    @router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
    async def create(payload: ProjectCreate, session: Session = Depends(get_session)):
        fields = payload.model_dump()
        fields["default_linked_workspaces"] = _coerce_workspaces(
            payload.default_linked_workspaces
        )
        try:
            return create_project(session, **fields)
        except ProjectNameTaken:
            raise HTTPException(409, "name or slug taken")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.get("", response_model=list[ProjectOut])
    async def lst(
        archived: bool = Query(default=False),
        session: Session = Depends(get_session),
    ):
        return list_projects(session, include_archived=archived)

    @router.get("/{project_id}", response_model=ProjectOut)
    async def show(project_id: str, session: Session = Depends(get_session)):
        try:
            return get_project(session, project_id)
        except ProjectNotFound:
            raise HTTPException(404, "not found")

    @router.patch("/{project_id}", response_model=ProjectOut)
    async def update(
        project_id: str,
        payload: ProjectUpdate,
        session: Session = Depends(get_session),
    ):
        fields = payload.model_dump(exclude_unset=True)
        if "default_linked_workspaces" in fields:
            fields["default_linked_workspaces"] = _coerce_workspaces(
                payload.default_linked_workspaces
            )
        try:
            return update_project(session, project_id, **fields)
        except ProjectNotFound:
            raise HTTPException(404, "not found")
        except ProjectNameTaken:
            raise HTTPException(409, "name or slug taken")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete(project_id: str, session: Session = Depends(get_session)):
        try:
            archive_project(session, project_id)
        except ProjectNotFound:
            raise HTTPException(404, "not found")

    return router
