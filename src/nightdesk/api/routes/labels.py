"""JSON API routes for labels CRUD and ticket-label association."""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from nightdesk.domain import scopes as sc
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

_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{3,8}$')


def _check_color(v: Optional[str]) -> Optional[str]:
    if v and not _COLOR_RE.match(v):
        raise ValueError(
            f"color must be empty or match #[0-9a-fA-F]{{3,8}}, got: {v!r}"
        )
    return v


class LabelCreate(BaseModel):
    name: str
    color: str = ""

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str) -> str:
        return _check_color(v) or ""


class LabelUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: Optional[str]) -> Optional[str]:
        return _check_color(v)


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

def build_router(get_session, bearer_token: str, scoped) -> APIRouter:
    router = APIRouter(prefix="/api/v1/labels", tags=["labels"])
    # Reading labels rides tickets.read; label entity CRUD needs labels.write.
    auth = Depends(scoped(sc.TICKETS_READ))
    write = Depends(scoped(sc.LABELS_WRITE))

    @router.get("", dependencies=[auth])
    async def list_all(session: Session = Depends(get_session)):
        labels = list_labels(session)
        return [_label_to_dict(l) for l in labels]

    @router.post("", dependencies=[write], status_code=201)
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

    @router.patch("/{label_id}", dependencies=[write])
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

    @router.delete("/{label_id}", dependencies=[write], status_code=204)
    async def delete(label_id: str, session: Session = Depends(get_session)):
        try:
            delete_label(session, label_id)
        except LabelNotFound:
            raise HTTPException(404, "label not found")
        return None

    # --- Ticket ↔ Label association -----------------------------------------

    @router.put(
        "/tickets/{ticket_id}",
        dependencies=[write],
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
