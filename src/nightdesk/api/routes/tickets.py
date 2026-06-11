from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_bearer
from nightdesk.domain.projects import ProjectNotFound
from nightdesk.api.schemas import (
    BulkPriorityUpdate, BulkProfileUpdate, BulkProjectUpdate, BulkStatusUpdate,
    BulkUpdateResult,
    DependencyCreate, DependencyOut,
    TicketCreate, TicketOut, TicketPriorityUpdate, TicketProfileUpdate,
    TicketProjectUpdate, TicketReorder, TicketStatusUpdate, TicketTransition,
    TicketUpdate,
)
from nightdesk.domain.tickets import (
    add_dependency, archive, bulk_update_priority, bulk_update_profile,
    bulk_update_project, bulk_update_status, create_ticket, delete_ticket,
    get_ticket, list_tickets, remove_dependency, requeue,
    reorder_in_column, request_run_now, transition_status,
    transition_with_position, unarchive, update_ticket,
    update_ticket_priority, update_ticket_profile, update_ticket_project,
    TicketNotFound, InvalidTransition, CyclicDependency, DependencyNotFound,
)
from nightdesk.domain.profiles import ProfileNotFound


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
    labels = [
        {"id": lbl.id, "name": lbl.name, "color": lbl.color}
        for lbl in (t.labels or [])
    ]
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
        "labels": labels,
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

    # --- Bulk metadata update endpoints -----------------------------------------
    # Registered BEFORE /{tid} routes so FastAPI does not match "bulk" as a tid.

    @router.patch("/bulk/priority", response_model=BulkUpdateResult)
    async def bulk_priority(
        payload: BulkPriorityUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            updated, skipped = bulk_update_priority(
                session, payload.ticket_ids, payload.priority,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        return BulkUpdateResult(
            updated=[_ticket_to_out(t) for t in updated],
            skipped=skipped,
        )

    @router.patch("/bulk/status", response_model=BulkUpdateResult)
    async def bulk_status(
        payload: BulkStatusUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            updated, skipped = bulk_update_status(
                session, payload.ticket_ids, payload.status,
            )
        except InvalidTransition as e:
            raise HTTPException(422, str(e))
        return BulkUpdateResult(
            updated=[_ticket_to_out(t) for t in updated],
            skipped=skipped,
        )

    @router.patch("/bulk/project", response_model=BulkUpdateResult)
    async def bulk_project(
        payload: BulkProjectUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            updated, skipped = bulk_update_project(
                session, payload.ticket_ids, payload.project_id,
            )
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        return BulkUpdateResult(
            updated=[_ticket_to_out(t) for t in updated],
            skipped=skipped,
        )

    @router.patch("/bulk/profile", response_model=BulkUpdateResult)
    async def bulk_profile(
        payload: BulkProfileUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            updated, skipped = bulk_update_profile(
                session, payload.ticket_ids, payload.profile_id,
            )
        except ProfileNotFound:
            raise HTTPException(404, "profile not found")
        return BulkUpdateResult(
            updated=[_ticket_to_out(t) for t in updated],
            skipped=skipped,
        )

    @router.post("/reorder", response_model=list[TicketOut])
    async def reorder(payload: TicketReorder, session: Session = Depends(get_session)):
        try:
            tickets = reorder_in_column(session, payload.status, payload.ticket_ids)
            return [_ticket_to_out(t) for t in tickets]
        except InvalidTransition as e:
            raise HTTPException(422, str(e))

    # --- Per-ticket routes (path parameter {tid}) --------------------------------

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

    # --- Focused metadata update endpoints --------------------------------------
    # Lightweight property-edit routes for the property picker, list inline
    # edits, keyboard actions, and bulk operations.  Each touches exactly one
    # field so callers don't need to construct a full TicketUpdate just to
    # change the priority.  The existing full PATCH endpoint is preserved.

    @router.patch("/{tid}/priority", response_model=TicketOut)
    async def update_priority(
        tid: str, payload: TicketPriorityUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            t = update_ticket_priority(session, tid, payload.priority)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.patch("/{tid}/status", response_model=TicketOut)
    async def update_status(
        tid: str, payload: TicketStatusUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            t = transition_status(session, tid, payload.status)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.patch("/{tid}/project", response_model=TicketOut)
    async def update_project(
        tid: str, payload: TicketProjectUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            t = update_ticket_project(session, tid, payload.project_id)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ProjectNotFound:
            raise HTTPException(404, "project not found")

    @router.patch("/{tid}/profile", response_model=TicketOut)
    async def update_profile(
        tid: str, payload: TicketProfileUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            t = update_ticket_profile(session, tid, payload.profile_id)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ProfileNotFound:
            raise HTTPException(404, "profile not found")

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
