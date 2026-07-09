"""Ticket transition events + the acting principal (Layer 0 of the
post-review acknowledgement flow — docs/design/post-review-acknowledge-flow.md).

Every ticket status change funnels through
``domain.tickets.transition_with_position``; that function calls
``record_transition_event`` here to append one :class:`~nightdesk.db.models.TicketEvent`
and apply the acknowledgement rules. Keeping both in one helper means the audit
log and the ack flag can never drift from the actual transition.

Actor plumbing
--------------
An :class:`Actor` is who caused a transition. The API resolves it from the
request principal via :func:`actor_from_principal`; the worker constructs a
``run`` actor around its own transitions. The default everywhere is
:data:`ADMIN` — correct for the human-driven API surface, which is the only
surface open today (run tokens cannot reach the ticket transition routes yet).

Seam for scoped agent tokens: when the sibling ``agent-token-permissions``
design lands a ``TokenPrincipal``, :func:`actor_from_principal` maps it to
``kind="token"`` and the routes it opens pass the resolved actor down — no
change to the event/ack machinery.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from nightdesk.db.models import Ticket, TicketEvent


# States that mean "work is in motion or awaiting dispatch". Transitioning INTO
# one of these clears any acknowledgement: the ticket's next outcome is unseen
# again (this is what makes requeue/unarchive drop the ack per the design).
ACTIVE_STATES: frozenset[str] = frozenset({"inbox", "draft", "queued", "running"})


@dataclass(frozen=True)
class Actor:
    """Who caused a ticket transition.

    ``kind`` is ``admin`` | ``run`` | ``token``. Only ``admin`` acknowledges;
    ``run``/``token`` are agent principals whose actions must never mark a
    ticket seen.
    """

    kind: str = "admin"
    run_id: Optional[str] = None
    session_id: Optional[str] = None


# The default actor for every domain caller that doesn't pass one. The API's
# human surface (admin bearer/cookie) is the only transition surface open today,
# so admin-by-default is correct; the worker and future token routes override.
ADMIN = Actor(kind="admin")


def run_actor(run_id: Optional[str]) -> Actor:
    """A ``run`` actor for worker-internal transitions on a run's behalf."""
    return Actor(kind="run", run_id=run_id)


def actor_from_principal(principal) -> Actor:
    """Map an auth principal onto an :class:`Actor`.

    - ``AdminPrincipal`` -> ``admin``.
    - ``RunPrincipal`` -> ``run`` carrying its ``run_id``.
    - Anything else (the future ``TokenPrincipal``) -> ``token`` carrying a
      ``session_id`` if it exposes one. This branch never produces that case;
      it is the merge seam for the scoped-agent-token work.
    """
    kind = getattr(principal, "kind", None)
    if kind == "admin":
        return ADMIN
    run_id = getattr(principal, "run_id", None)
    if run_id is not None:
        return Actor(kind="run", run_id=run_id)
    session_id = getattr(principal, "session_id", None)
    return Actor(kind="token", session_id=session_id)


def record_transition_event(
    session: Session,
    ticket: Ticket,
    *,
    from_status: Optional[str],
    to_status: str,
    actor: Actor,
) -> TicketEvent:
    """Append a transition event and apply the acknowledgement rules.

    Mutates ``ticket`` (ack columns) and adds a :class:`TicketEvent` to the
    session but does NOT commit — the caller
    (``transition_with_position``) owns the commit so the event, the status
    change, and the ack update land atomically.

    Ack rules (design Layer 1), evaluated in order:

    1. Transitioning into an :data:`ACTIVE_STATES` state clears the ack — the
       ticket re-entered motion, so its next outcome is unseen. This is why an
       admin requeue (review -> queued) drops the ack even though the human
       touched it.
    2. Otherwise, an ``admin`` actor sets the ack (the human saw this outcome
       by acting on it).
    3. Otherwise (a run/token moving into review/archived) the ack is left
       untouched — it stays NULL and the ticket surfaces in the digest.
    """
    event = TicketEvent(
        ticket_id=ticket.id,
        from_status=from_status,
        to_status=to_status,
        actor_kind=actor.kind,
        run_id=actor.run_id,
        session_id=actor.session_id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(event)

    if to_status in ACTIVE_STATES:
        ticket.acknowledged_at = None
        ticket.acknowledged_by = None
    elif actor.kind == "admin":
        ticket.acknowledged_at = datetime.now(timezone.utc)
        ticket.acknowledged_by = "admin"
    return event
