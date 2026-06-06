"""JSON API routes for labels CRUD and ticket-label association."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.domain.labels import (
    LabelNameTaken,
    LabelNotFound,
    create_label,
    delete_label,
    get_label,
    list_labels,
    set_ticket_labels,
    update_label,
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LabelCreate(BaseModel):
    name: str
    color: str = ""


class LabelUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None


class TicketLabelsUpdate(BaseModel):
    label_ids: list[str]


def _label_to_dict(label) -> dict:
    return {
        "id": label.id,
        "name": label.name,
        "color": label.color,
        "created_at": label.created_at.isoformat() if label.created_at else None,
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def build_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/v1/labels", tags=["labels"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("", dependencies=[auth])
    async def list_all(session: Session = Depends(get_session)):
        labels = list_labels(session)
        return [_label_to_dict(l) for l in labels]

    @router.post("", dependencies=[auth], status_code=201)
    async def create(body: LabelCreate, session: Session = Depends(get_session)):
        try:
            label = create_label(session, name=body.name, color=body.color)
        except LabelNameTaken as e:
            raise HTTPException(409, f"label name already taken: {e}")
        return _label_to_dict(label)

    @router.get("/{label_id}", dependencies=[auth])
    async def get_one(label_id: str, session: Session = Depends(get_session)):
        try:
            label = get_label(session, label_id)
        except LabelNotFound:
            raise HTTPException(404, "label not found")
        return _label_to_dict(label)

    @router.patch("/{label_id}", dependencies=[auth])
    async def update(
        label_id: str,
        body: LabelUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            label = update_label(
                session, label_id,
                name=body.name, color=body.color,
            )
        except LabelNotFound:
            raise HTTPException(404, "label not found")
        except LabelNameTaken as e:
            raise HTTPException(409, f"label name already taken: {e}")
        return _label_to_dict(label)

    @router.delete("/{label_id}", dependencies=[auth], status_code=204)
    async def delete(label_id: str, session: Session = Depends(get_session)):
        try:
            delete_label(session, label_id)
        except LabelNotFound:
            raise HTTPException(404, "label not found")
        return None

    # --- Ticket ↔ Label association -----------------------------------------

    @router.put(
        "/tickets/{ticket_id}",
        dependencies=[auth],
    )
    async def set_labels(
        ticket_id: str,
        body: TicketLabelsUpdate,
        session: Session = Depends(get_session),
    ):
        from nightdesk.domain.tickets import TicketNotFound
        try:
            ticket = set_ticket_labels(session, ticket_id, body.label_ids)
        except TicketNotFound:
            raise HTTPException(404, "ticket not found")
        except LabelNotFound as e:
            raise HTTPException(404, str(e))
        return [_label_to_dict(l) for l in ticket.labels]

    return router
