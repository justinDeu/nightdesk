"""Inbox: triage surface for under-specified work (JSON API).

The inbox is a dedicated surface, separate from the runnable board, where
captured-but-under-specified tickets (``status='inbox'``) wait to be triaged.
From here a ticket is promoted (accepted) onto the board as ``draft`` or
``queued`` directly, or declined (archived). Promotion crosses the
incomplete-ticket validation boundary in ``domain.tickets`` — an item
missing a workspace cannot be promoted until it is fleshed out, but it can
always be declined. Capture reuses ``POST /api/v1/tickets`` with
``status="inbox"``; there is no separate capture endpoint here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from nightdesk.domain import scopes as sc
from nightdesk.api.schemas import InboxCountOut, InboxItemOut, TicketPromote
from nightdesk.domain.tickets import (
    IncompleteTicket,
    InvalidTransition,
    TicketNotFound,
    decline_ticket,
    list_inbox,
    promote_ticket,
    ticket_completeness,
)


def build_api_router(get_session, bearer_token: str, scoped) -> APIRouter:
    """JSON /api/v1/inbox — triage list, promote, decline, unread count."""
    from nightdesk.api.routes.tickets import _ticket_to_out

    router = APIRouter(prefix="/api/v1", tags=["inbox-api"])
    auth = Depends(scoped(sc.TICKETS_READ))
    transition = Depends(scoped(sc.TICKETS_TRANSITION))

    @router.get("/inbox", response_model=list[InboxItemOut], dependencies=[auth])
    async def inbox_list_api(
        project_id: str | None = Query(default=None),
        session: Session = Depends(get_session),
    ):
        tickets = list_inbox(session, project_id=project_id)
        return [
            InboxItemOut(ticket=_ticket_to_out(t), blockers=ticket_completeness(t))
            for t in tickets
        ]

    @router.get("/inbox/count", response_model=InboxCountOut, dependencies=[auth])
    async def inbox_count_api(session: Session = Depends(get_session)):
        return InboxCountOut(count=len(list_inbox(session, limit=10_000)))

    @router.post("/tickets/{tid}/promote", dependencies=[transition])
    async def promote_api(
        tid: str, payload: TicketPromote,
        session: Session = Depends(get_session),
    ):
        try:
            t = promote_ticket(session, tid, payload.target)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except IncompleteTicket as e:
            raise HTTPException(422, "; ".join(e.reasons))
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return _ticket_to_out(t)

    @router.post("/tickets/{tid}/decline", dependencies=[transition])
    async def decline_api(tid: str, session: Session = Depends(get_session)):
        try:
            t = decline_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return _ticket_to_out(t)

    return router
