"""Acknowledgement mechanism + digest query (Layers 1-2 of the post-review
acknowledgement flow — docs/design/post-review-acknowledge-flow.md).

The ack flag itself is written in two ways: implicitly by human transitions
(see ``domain.events.record_transition_event``) and explicitly by the functions
here (``acknowledge_ticket`` / ``bulk_acknowledge``), which back the admin-only
ack endpoints. Acknowledging is deliberately NOT a transition — it never moves
the ticket or writes a ``ticket_event`` — so an agent can archive but can never
mark its own work seen.

The digest (``ack_digest``) is the read side: archived tickets with
``acknowledged_at IS NULL``, grouped by project and by the day they were
archived. The "entered at" timestamp comes from
``ticket_events`` (a real archived-at), not ``tickets.updated_at``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import Run, Ticket, TicketEvent


# Review tickets may be acknowledged early from Needs-you, but only decided
# tickets belong in the acknowledgement digest.
ACKNOWLEDGEABLE_STATUSES: tuple[str, ...] = ("review", "archived")
DIGEST_STATUSES: tuple[str, ...] = ("archived",)


class TicketNotAcknowledgeable(Exception):
    """Raised when a single-ticket ack targets a ticket that is not in a
    terminal (review/archived) state — there is no outcome to acknowledge."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def acknowledge_ticket(
    session: Session, ticket_id: str, *, by: str = "admin",
) -> Ticket:
    """Mark one ticket's current outcome as seen by a human.

    Idempotent: acking an already-acked ticket refreshes the stamp. Only
    review/archived tickets can be acked — an active ticket has no settled
    outcome to acknowledge (raises :class:`TicketNotAcknowledgeable`).
    """
    from nightdesk.domain.tickets import get_ticket

    t = get_ticket(session, ticket_id)
    if t.status not in ACKNOWLEDGEABLE_STATUSES:
        raise TicketNotAcknowledgeable(
            f"cannot acknowledge a ticket in '{t.status}'"
        )
    t.acknowledged_at = _now()
    t.acknowledged_by = by
    session.commit()
    session.refresh(t)
    return t


_UNSET = object()


def bulk_acknowledge(
    session: Session,
    *,
    ticket_ids: Optional[list[str]] = None,
    project_id=_UNSET,
    before: Optional[datetime] = None,
    by: str = "admin",
) -> list[str]:
    """Acknowledge a batch, returning the ids actually acked.

    Two selection modes (exactly one is expected):

    - ``ticket_ids``: ack these specific tickets (the ones the human selected).
    - ``project_id``: ack every unacked review/archived ticket in that project.
      A real id scopes to the project; ``None`` scopes to the no-project group;
      leaving it unset means "not project-scoped".

    Common filters always apply: a ticket is only acked if it is currently in a
    digest state and unacknowledged. ``before`` (typically the timestamp the
    digest was rendered) makes "ack all" race-safe: tickets that entered their
    terminal state AFTER ``before`` — i.e. work that landed while the human was
    reading — are left for the next pass rather than silently swallowed.
    """
    stmt = select(Ticket).where(
        Ticket.status.in_(ACKNOWLEDGEABLE_STATUSES),
        Ticket.acknowledged_at.is_(None),
    )
    if ticket_ids is not None:
        if not ticket_ids:
            return []
        stmt = stmt.where(Ticket.id.in_(ticket_ids))
    elif project_id is not _UNSET:
        if project_id is None:
            stmt = stmt.where(Ticket.project_id.is_(None))
        else:
            stmt = stmt.where(Ticket.project_id == project_id)

    candidates = list(session.scalars(stmt))
    if not candidates:
        return []

    entered = _entering_events(session, candidates)
    now = _now()
    acked: list[str] = []
    for t in candidates:
        if before is not None:
            ev = entered.get(t.id)
            entered_at = _aware(ev.created_at) if ev is not None else None
            if entered_at is not None and entered_at > before:
                continue
        t.acknowledged_at = now
        t.acknowledged_by = by
        acked.append(t.id)
    session.commit()
    return acked


# ---------------------------------------------------------------------------
# Read side: the digest + supporting event lookups.
# ---------------------------------------------------------------------------


@dataclass
class DigestTicket:
    ticket_id: str
    title: str
    # Human-facing summary; the digest UI prefers this over ``title``.
    description: Optional[str]
    status: str
    project_id: Optional[str]
    entered_at: Optional[datetime]
    # Who moved it into its current status: 'admin' | 'run' | 'token' | None.
    actor_kind: Optional[str]
    outcome: Optional[str]           # 'succeeded' | 'failed' | None (never ran)
    cost_usd: Optional[float]
    run_id: Optional[str]


@dataclass
class DigestGroup:
    project_id: Optional[str]
    day: date
    tickets: list[DigestTicket] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.tickets)

    @property
    def succeeded(self) -> int:
        return sum(1 for t in self.tickets if t.outcome == "succeeded")

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tickets if t.outcome == "failed")

    @property
    def cost_usd(self) -> float:
        return sum(t.cost_usd or 0.0 for t in self.tickets)


@dataclass
class Digest:
    total: int
    groups: list[DigestGroup]
    # Watermark the client passes back as ``before`` on "ack all" for race
    # safety. Captured when the digest was computed.
    generated_at: datetime


def ack_digest(session: Session, *, project_id: Optional[str] = None) -> Digest:
    """Build the unacknowledged-work digest, grouped by project then day.

    Only decided (archived) tickets appear; review tickets remain in Needs-you.
    ``project_id`` optionally scopes to one project (or the no-project group is
    NOT special-cased here — pass a real id to scope; omit for everything).
    Groups are sorted newest day first, and tickets within a group newest first.
    """
    stmt = select(Ticket).where(
        Ticket.status.in_(DIGEST_STATUSES),
        Ticket.acknowledged_at.is_(None),
    )
    if project_id is not None:
        stmt = stmt.where(Ticket.project_id == project_id)
    tickets = list(session.scalars(stmt))

    entered = _entering_events(session, tickets)
    latest_runs = _latest_runs(session, tickets)

    groups: dict[tuple[Optional[str], date], DigestGroup] = {}
    for t in tickets:
        ev = entered.get(t.id)
        entered_at = _aware(ev.created_at) if ev is not None else _aware(t.updated_at)
        day = entered_at.date() if entered_at is not None else _now().date()
        run = latest_runs.get(t.id)
        outcome = None
        if run is not None and run.exit_status is not None:
            outcome = "succeeded" if run.exit_status == "success" else "failed"
        dt = DigestTicket(
            ticket_id=t.id,
            title=t.title,
            description=t.description,
            status=t.status,
            project_id=t.project_id,
            entered_at=entered_at,
            actor_kind=ev.actor_kind if ev is not None else None,
            outcome=outcome,
            cost_usd=run.cost_usd if run is not None else None,
            run_id=run.id if run is not None else None,
        )
        key = (t.project_id, day)
        groups.setdefault(key, DigestGroup(project_id=t.project_id, day=day)).tickets.append(dt)

    for g in groups.values():
        g.tickets.sort(
            key=lambda d: (d.entered_at or _now()), reverse=True,
        )
    ordered = sorted(groups.values(), key=lambda g: g.day, reverse=True)
    return Digest(total=len(tickets), groups=ordered, generated_at=_now())


def unacknowledged_count(session: Session) -> int:
    """Cheap count for the Desk band header (no grouping/joins)."""
    from sqlalchemy import func

    return int(
        session.scalar(
            select(func.count(Ticket.id)).where(
                Ticket.status.in_(DIGEST_STATUSES),
                Ticket.acknowledged_at.is_(None),
            )
        )
        or 0
    )


def entering_event(session: Session, ticket: Ticket) -> Optional[TicketEvent]:
    """The latest event that moved ``ticket`` into its CURRENT status.

    This is the ticket's real "archived at" / "entered review at" and carries
    the actor who did it (the agent-reviewed chip reads ``actor_kind`` off it).
    Falls back to None when no matching event exists (pre-0028 tickets).
    """
    return _entering_events(session, [ticket]).get(ticket.id)


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------


def _entering_events(
    session: Session, tickets: list[Ticket],
) -> dict[str, TicketEvent]:
    """For each ticket, the latest ``ticket_event`` whose ``to_status`` matches
    the ticket's current status. One batched query, resolved in Python."""
    if not tickets:
        return {}
    by_id = {t.id: t for t in tickets}
    rows = session.scalars(
        select(TicketEvent)
        .where(TicketEvent.ticket_id.in_(list(by_id)))
        .order_by(TicketEvent.created_at.asc(), TicketEvent.id.asc())
    )
    out: dict[str, TicketEvent] = {}
    for ev in rows:
        t = by_id.get(ev.ticket_id)
        if t is not None and ev.to_status == t.status:
            out[ev.ticket_id] = ev  # ascending order => last match wins (latest)
    return out


def _latest_runs(session: Session, tickets: list[Ticket]) -> dict[str, Run]:
    """Latest run per ticket by ``started_at`` (id breaks ties), batched."""
    if not tickets:
        return {}
    ids = [t.id for t in tickets]
    rows = session.scalars(
        select(Run)
        .where(Run.ticket_id.in_(ids))
        .order_by(Run.started_at.desc(), Run.id.desc())
    )
    out: dict[str, Run] = {}
    for r in rows:
        out.setdefault(r.ticket_id, r)  # first seen == latest
    return out


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite drops tzinfo; treat naive stored datetimes as UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
