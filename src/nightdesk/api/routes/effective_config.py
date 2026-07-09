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

from nightdesk.domain import scopes as sc
from nightdesk.domain.effective_config import (
    resolve_for_draft,
    resolve_for_ticket,
)
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.tickets import get_ticket, TicketNotFound


def build_api_router(get_session, bearer_token: str, scoped) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["effective-config"])
    auth = Depends(scoped(sc.TICKETS_READ))
    # Needed by the "Launch plan" group to resolve endpoint credentials for
    # the dry-run render (masked before they reach the response either way —
    # see domain.effective_config._launch_plan_group).
    secret_box = ProfileSecretBox(bearer_token) if bearer_token else None

    @router.get("/tickets/{tid}/effective-config", dependencies=[auth])
    async def ticket_effective_config(tid: str, session: Session = Depends(get_session)):
        try:
            ticket = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "ticket not found")
        return resolve_for_ticket(session, ticket, secret_box).as_dict()

    @router.post("/effective-config/preview", dependencies=[auth])
    async def preview_effective_config(
        payload: dict = Body(default_factory=dict),
        session: Session = Depends(get_session),
    ):
        return resolve_for_draft(session, payload or {}, secret_box).as_dict()

    return router
