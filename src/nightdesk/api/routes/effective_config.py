"""Effective execution-context resolver endpoints.

JSON-only, over one resolver (:mod:`nightdesk.domain.effective_config`):

* ``GET /api/v1/tickets/{tid}/effective-config`` — resolved context for an
  existing ticket.
* ``POST /api/v1/effective-config/preview`` — resolved context for a draft
  (not-yet-created) ticket; accepts the same fields ``create_ticket`` does,
  so a preview reflects exactly what would be persisted (project defaults
  are applied).
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.domain.effective_config import (
    resolve_for_draft,
    resolve_for_ticket,
)
from nightdesk.domain.tickets import get_ticket, TicketNotFound


def build_api_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["effective-config"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("/tickets/{tid}/effective-config", dependencies=[auth])
    async def ticket_effective_config(tid: str, session: Session = Depends(get_session)):
        try:
            ticket = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "ticket not found")
        return resolve_for_ticket(session, ticket).as_dict()

    @router.post("/effective-config/preview", dependencies=[auth])
    async def preview_effective_config(
        payload: dict = Body(default_factory=dict),
        session: Session = Depends(get_session),
    ):
        return resolve_for_draft(session, payload or {}).as_dict()

    return router
