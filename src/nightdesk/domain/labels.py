"""Label CRUD and ticket-label association.

Labels are simple named+colored tags.  They are intentionally decoupled from
the ticket lifecycle: creating, deleting or re-naming a label does not affect
ticket status.  The only mutable link is the many-to-many ``ticket_labels``
join table, managed via ``set_ticket_labels``.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from nightdesk.db.models import Label, Ticket, ticket_labels


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class LabelNotFound(Exception):
    pass


class LabelNameTaken(Exception):
    pass


# ---------------------------------------------------------------------------
# Label CRUD
# ---------------------------------------------------------------------------

def create_label(session: Session, *, name: str, color: str = "") -> Label:
    """Create a new label.  Raises ``LabelNameTaken`` on duplicate name."""
    existing = session.scalar(select(Label).where(Label.name == name))
    if existing is not None:
        raise LabelNameTaken(name)
    label = Label(name=name, color=color)
    session.add(label)
    session.commit()
    session.refresh(label)
    return label


def get_label(session: Session, label_id: str) -> Label:
    label = session.get(Label, label_id)
    if label is None:
        raise LabelNotFound(label_id)
    return label


def list_labels(session: Session) -> list[Label]:
    """All labels, ordered by name."""
    return list(session.scalars(select(Label).order_by(Label.name.asc())))


def update_label(
    session: Session,
    label_id: str,
    *,
    name: Optional[str] = None,
    color: Optional[str] = None,
) -> Label:
    label = get_label(session, label_id)
    if name is not None and name != label.name:
        existing = session.scalar(select(Label).where(Label.name == name))
        if existing is not None:
            raise LabelNameTaken(name)
        label.name = name
    if color is not None:
        label.color = color
    session.commit()
    session.refresh(label)
    return label


def delete_label(session: Session, label_id: str) -> None:
    label = get_label(session, label_id)
    session.delete(label)
    session.commit()


# ---------------------------------------------------------------------------
# Ticket ↔ Label association
# ---------------------------------------------------------------------------

def set_ticket_labels(session: Session, ticket_id: str, label_ids: list[str]) -> Ticket:
    """Replace a ticket's labels with the given set of label IDs."""
    from nightdesk.domain.tickets import get_ticket
    ticket = get_ticket(session, ticket_id)
    # Validate all label IDs exist
    if label_ids:
        found = session.scalars(
            select(Label).where(Label.id.in_(label_ids))
        ).all()
        if len(found) != len(label_ids):
            missing = set(label_ids) - {l.id for l in found}
            raise LabelNotFound(f"label(s) not found: {', '.join(missing)}")
    labels = list(session.scalars(
        select(Label).where(Label.id.in_(label_ids))
    )) if label_ids else []
    ticket.labels = labels
    session.commit()
    session.refresh(ticket)
    return ticket


def get_ticket_labels(session: Session, ticket_id: str) -> list[Label]:
    """Get labels for a single ticket."""
    from nightdesk.domain.tickets import get_ticket
    ticket = get_ticket(session, ticket_id)
    return list(ticket.labels)


def tickets_for_label(session: Session, label_id: str) -> list[Ticket]:
    """All tickets that have the given label."""
    label = get_label(session, label_id)
    return list(label.tickets)
