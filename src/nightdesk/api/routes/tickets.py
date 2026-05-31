from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_bearer
from nightdesk.domain.projects import ProjectNotFound
from nightdesk.api.schemas import (
    DependencyCreate, DependencyOut,
    TicketCreate, TicketOut, TicketReorder, TicketTransition, TicketUpdate,
)
from nightdesk.domain.tickets import (
    add_dependency, archive, create_ticket, delete_ticket, get_ticket,
    list_tickets, remove_dependency, requeue,
    reorder_in_column, request_run_now, transition_status,
    transition_with_position, unarchive, update_ticket,
    TicketNotFound, InvalidTransition, CyclicDependency, DependencyNotFound,
)


def _coerce_dirs(payload_dirs):
    """Convert AdditionalDir pydantic models to plain dicts for JSON storage."""
    if payload_dirs is None:
        return None
    return [
        d.model_dump() if hasattr(d, "model_dump") else dict(d) for d in payload_dirs
    ]


def _coerce_workspaces(payload_workspaces):
    if payload_workspaces is None:
        return None
    return [
        w.model_dump() if hasattr(w, "model_dump") else dict(w)
        for w in payload_workspaces
    ]


def _ticket_to_out(t) -> TicketOut:
    """Build a TicketOut including dependency edges."""
    from nightdesk.api.schemas import TicketWorkspaceOut
    deps = []
    for dep in (t.dependencies or []):
        upstream = dep.depends_on
        deps.append(DependencyOut(
            id=dep.id,
            ticket_id=t.id,
            depends_on_id=dep.depends_on_id,
            depends_on_title=upstream.title if upstream else "(deleted)",
            depends_on_status=upstream.status if upstream else "unknown",
            created_at=dep.created_at,
        ))
    workspaces = []
    for ws in (t.workspaces or []):
        workspaces.append(TicketWorkspaceOut.model_validate(ws))
    data = {
        "id": t.id,
        "title": t.title,
        "prompt": t.prompt,
        "status": t.status,
        "priority": t.priority,
        "position": t.position,
        "project_id": t.project_id,
        "profile_id": t.profile_id,
        "permission_overrides": t.permission_overrides,
        "toolchain_overrides": t.toolchain_overrides,
        "additional_dirs": t.additional_dirs or [],
        "workspaces": workspaces,
        "run_now": t.run_now,
        "scheduled_after": t.scheduled_after,
        "current_run_id": t.current_run_id,
        "next_run_context": t.next_run_context,
        "next_run_context_updated_at": t.next_run_context_updated_at,
        "dependencies": deps,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }
    return TicketOut(**data)


def build_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/tickets",
        tags=["tickets"],
        dependencies=[Depends(require_bearer(bearer_token))],
    )

    @router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
    async def create(payload: TicketCreate, session: Session = Depends(get_session)):
        data = payload.model_dump()
        # If caller didn't specify status, let domain default it (to 'draft').
        if data.get("status") is None:
            data.pop("status", None)
        if "workspace_mode" not in payload.model_fields_set:
            data.pop("workspace_mode", None)
        data["additional_dirs"] = _coerce_dirs(payload.additional_dirs) or []
        data["workspaces"] = _coerce_workspaces(payload.workspaces)
        try:
            t = create_ticket(session, **data)
            return _ticket_to_out(t)
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        except (InvalidTransition, ValueError) as e:
            raise HTTPException(422, str(e))

    @router.get("", response_model=list[TicketOut])
    async def lst(
        status: str | None = Query(default=None),
        profile_id: str | None = Query(default=None),
        project_id: str | None = Query(default=None),
        session: Session = Depends(get_session),
    ):
        tickets = list_tickets(session, status=status, profile_id=profile_id, project_id=project_id)
        return [_ticket_to_out(t) for t in tickets]

    @router.get("/{tid}", response_model=TicketOut)
    async def show(tid: str, session: Session = Depends(get_session)):
        try:
            t = get_ticket(session, tid)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")

    @router.patch("/{tid}", response_model=TicketOut)
    async def update(tid: str, payload: TicketUpdate, session: Session = Depends(get_session)):
        data = payload.model_dump()
        fields = {k: v for k, v in data.items() if v is not None}
        if "project_id" in payload.model_fields_set:
            fields["project_id"] = data["project_id"]
        if "additional_dirs" in fields:
            fields["additional_dirs"] = _coerce_dirs(payload.additional_dirs) or []
        if "workspaces" in fields:
            fields["workspaces"] = _coerce_workspaces(payload.workspaces)
        try:
            t = update_ticket(session, tid, **fields)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.post("/{tid}/run-now", response_model=TicketOut)
    async def run_now(tid: str, session: Session = Depends(get_session)):
        try:
            t = request_run_now(session, tid)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/cancel", response_model=TicketOut)
    async def cancel(tid: str, session: Session = Depends(get_session)):
        try:
            t = transition_status(session, tid, "review")
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/requeue", response_model=TicketOut)
    async def requeue_route(tid: str, session: Session = Depends(get_session)):
        try:
            t = requeue(session, tid)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/transition", response_model=TicketOut)
    async def transition(
        tid: str, payload: TicketTransition,
        session: Session = Depends(get_session),
    ):
        try:
            t = transition_with_position(
                session, tid, payload.status, position=payload.position
            )
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/reorder", response_model=list[TicketOut])
    async def reorder(payload: TicketReorder, session: Session = Depends(get_session)):
        try:
            tickets = reorder_in_column(session, payload.status, payload.ticket_ids)
            return [_ticket_to_out(t) for t in tickets]
        except InvalidTransition as e:
            raise HTTPException(422, str(e))

    @router.post("/{tid}/archive", response_model=TicketOut)
    async def archive_route(tid: str, session: Session = Depends(get_session)):
        try:
            t = archive(session, tid)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/unarchive", response_model=TicketOut)
    async def unarchive_route(tid: str, session: Session = Depends(get_session)):
        try:
            t = unarchive(session, tid)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.delete("/{tid}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete(tid: str, session: Session = Depends(get_session)):
        try:
            delete_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    # --- Dependency endpoints ---------------------------------------------------

    @router.get("/{tid}/dependencies", response_model=list[DependencyOut])
    async def list_deps(tid: str, session: Session = Depends(get_session)):
        try:
            t = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        out = []
        for dep in (t.dependencies or []):
            upstream = dep.depends_on
            out.append(DependencyOut(
                id=dep.id,
                ticket_id=t.id,
                depends_on_id=dep.depends_on_id,
                depends_on_title=upstream.title if upstream else "(deleted)",
                depends_on_status=upstream.status if upstream else "unknown",
                created_at=dep.created_at,
            ))
        return out

    @router.post("/{tid}/dependencies", response_model=DependencyOut,
                  status_code=status.HTTP_201_CREATED)
    async def add_dep(
        tid: str, payload: DependencyCreate,
        session: Session = Depends(get_session),
    ):
        try:
            t = add_dependency(session, tid, payload.depends_on_id)
        except TicketNotFound:
            raise HTTPException(404, "ticket not found")
        except CyclicDependency as e:
            raise HTTPException(422, str(e))
        # Find the newly added dependency.
        for dep in t.dependencies:
            if dep.depends_on_id == payload.depends_on_id:
                upstream = dep.depends_on
                return DependencyOut(
                    id=dep.id,
                    ticket_id=t.id,
                    depends_on_id=dep.depends_on_id,
                    depends_on_title=upstream.title if upstream else "(deleted)",
                    depends_on_status=upstream.status if upstream else "unknown",
                    created_at=dep.created_at,
                )
        raise HTTPException(500, "dependency was not created")

    @router.delete("/{tid}/dependencies/{dep_on_id}",
                    status_code=status.HTTP_204_NO_CONTENT)
    async def remove_dep(tid: str, dep_on_id: str,
                         session: Session = Depends(get_session)):
        try:
            remove_dependency(session, tid, dep_on_id)
        except DependencyNotFound:
            raise HTTPException(404, "dependency not found")

    return router
