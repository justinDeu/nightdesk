from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_bearer
from nightdesk.api.schemas import (
    TicketCreate, TicketOut, TicketReorder, TicketTransition, TicketUpdate,
)
from nightdesk.domain.tickets import (
    archive, create_ticket, delete_ticket, get_ticket, list_tickets, requeue,
    reorder_in_column, request_run_now, transition_status,
    transition_with_position, unarchive, update_ticket,
    TicketNotFound, InvalidTransition,
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
        data["additional_dirs"] = _coerce_dirs(payload.additional_dirs) or []
        data["workspaces"] = _coerce_workspaces(payload.workspaces)
        try:
            return create_ticket(session, **data)
        except (InvalidTransition, ValueError) as e:
            raise HTTPException(422, str(e))

    @router.get("", response_model=list[TicketOut])
    async def lst(
        status: str | None = Query(default=None),
        profile_id: str | None = Query(default=None),
        session: Session = Depends(get_session),
    ):
        return list_tickets(session, status=status, profile_id=profile_id)

    @router.get("/{tid}", response_model=TicketOut)
    async def show(tid: str, session: Session = Depends(get_session)):
        try:
            return get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")

    @router.patch("/{tid}", response_model=TicketOut)
    async def update(tid: str, payload: TicketUpdate, session: Session = Depends(get_session)):
        fields = {k: v for k, v in payload.model_dump().items() if v is not None}
        if "additional_dirs" in fields:
            fields["additional_dirs"] = _coerce_dirs(payload.additional_dirs) or []
        if "workspaces" in fields:
            fields["workspaces"] = _coerce_workspaces(payload.workspaces)
        try:
            return update_ticket(session, tid, **fields)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.post("/{tid}/run-now", response_model=TicketOut)
    async def run_now(tid: str, session: Session = Depends(get_session)):
        # Flipping the flag without also transitioning draft/review/archived
        # to queued is a silent no-op — the scheduler only picks tickets
        # matching status='queued' AND run_now=true. Use the helper so this
        # endpoint, the UI form, and drag-to-running all agree.
        try:
            return request_run_now(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/cancel", response_model=TicketOut)
    async def cancel(tid: str, session: Session = Depends(get_session)):
        # v2: cancel moves running -> review. Worker observes the change.
        try:
            return transition_status(session, tid, "review")
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/requeue", response_model=TicketOut)
    async def requeue_route(tid: str, session: Session = Depends(get_session)):
        try:
            return requeue(session, tid)
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
            return transition_with_position(
                session, tid, payload.status, position=payload.position
            )
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/reorder", response_model=list[TicketOut])
    async def reorder(payload: TicketReorder, session: Session = Depends(get_session)):
        try:
            return reorder_in_column(session, payload.status, payload.ticket_ids)
        except InvalidTransition as e:
            raise HTTPException(422, str(e))

    @router.post("/{tid}/archive", response_model=TicketOut)
    async def archive_route(tid: str, session: Session = Depends(get_session)):
        try:
            return archive(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/unarchive", response_model=TicketOut)
    async def unarchive_route(tid: str, session: Session = Depends(get_session)):
        try:
            return unarchive(session, tid)
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

    return router
