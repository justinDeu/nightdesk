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


def bulk_add_labels(
    session: Session,
    ticket_ids: list[str],
    label_ids: list[str],
) -> tuple[list[Ticket], list[dict]]:
    """Add the given labels to each ticket (set union — existing labels are
    preserved, duplicates are a no-op).  Tickets that don't exist are skipped.
    Returns ``(updated, skipped)`` where each skipped entry is
    ``{"ticket_id": ..., "reason": ...}``.

    Raises ``LabelNotFound`` once up front if any label id is unknown, so the
    whole batch fails fast rather than partially applying."""
    from nightdesk.domain.tickets import TicketNotFound, get_ticket

    label_ids = [lid for lid in label_ids if lid]
    if label_ids:
        found = session.scalars(
            select(Label).where(Label.id.in_(label_ids))
        ).all()
        if len(found) != len(set(label_ids)):
            missing = set(label_ids) - {lbl.id for lbl in found}
            raise LabelNotFound(f"label(s) not found: {', '.join(sorted(missing))}")
        labels_to_add = list(found)
    else:
        labels_to_add = []

    updated: list[Ticket] = []
    skipped: list[dict] = []
    for tid in ticket_ids:
        try:
            ticket = get_ticket(session, tid)
        except TicketNotFound:
            skipped.append({"ticket_id": tid, "reason": "not found"})
            continue
        existing = {lbl.id for lbl in ticket.labels}
        for lbl in labels_to_add:
            if lbl.id not in existing:
                ticket.labels.append(lbl)
        updated.append(ticket)
    if updated:
        session.commit()
        for t in updated:
            session.refresh(t)
    return updated, skipped


def bulk_remove_labels(
    session: Session,
    ticket_ids: list[str],
    label_ids: list[str],
) -> tuple[list[Ticket], list[dict]]:
    """Remove the given labels from each ticket. Missing ticket labels are a
    no-op; unknown label IDs fail the whole batch up front."""
    from nightdesk.domain.tickets import TicketNotFound, get_ticket

    label_ids = [lid for lid in label_ids if lid]
    if label_ids:
        found = session.scalars(
            select(Label).where(Label.id.in_(label_ids))
        ).all()
        if len(found) != len(set(label_ids)):
            missing = set(label_ids) - {lbl.id for lbl in found}
            raise LabelNotFound(f"label(s) not found: {', '.join(sorted(missing))}")

    remove_ids = set(label_ids)
    updated: list[Ticket] = []
    skipped: list[dict] = []
    for tid in ticket_ids:
        try:
            ticket = get_ticket(session, tid)
        except TicketNotFound:
            skipped.append({"ticket_id": tid, "reason": "not found"})
            continue
        ticket.labels = [lbl for lbl in ticket.labels if lbl.id not in remove_ids]
        updated.append(ticket)
    if updated:
        session.commit()
        for t in updated:
            session.refresh(t)
    return updated, skipped
