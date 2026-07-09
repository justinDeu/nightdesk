# src/nightdesk/worker/heartbeat.py
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import Run, Ticket, WorkerHeartbeat
from nightdesk.domain.events import record_transition_event, run_actor


log = logging.getLogger(__name__)


def _pid_alive(pid: int | None) -> bool:
    """True if ``pid`` names a live process. ``None`` -> unknown -> treat
    as alive (we don't want to kill runs that simply never recorded a pid).
    """
    if pid is None or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it (different uid). Still alive.
        return True
    except OSError:
        return True


def write_heartbeat(session: Session, *, host: str, pid: int) -> None:
    hb = session.get(WorkerHeartbeat, 1)
    now = datetime.now(timezone.utc)
    if hb is None:
        hb = WorkerHeartbeat(id=1, host=host, pid=pid, last_seen_at=now)
        session.add(hb)
    else:
        hb.host = host
        hb.pid = pid
        hb.last_seen_at = now
    session.commit()


def recover_orphaned_runs(session: Session, *, host: str) -> int:
    """Clean up tickets and runs left in flight by a dead worker.

    Two recovery passes:

    1. Tickets stuck in ``running`` with at least one unfinished ``Run`` on
       this host get those local runs marked ``worker_crash`` and the
       ticket pushed to ``review`` (unless another host still has an
       in-flight run for the same ticket).
    2. Tickets stuck in ``running`` with NO unfinished ``Run`` row at all
       (anywhere) — these were transitioned to running but the worker
       never managed to create a Run row. Reset them to ``queued`` so the
       scheduler can take another shot. Without this, the API's
       /board move endpoint or a crash mid-setup would wedge the ticket
       permanently.
    """
    now = datetime.now(timezone.utc)
    from sqlalchemy import exists
    from nightdesk.domain.conversations import sync_conversation_from_turn
    host_run_subq = (
        select(Run.id)
        .where(Run.ticket_id == Ticket.id, Run.finished_at.is_(None), Run.host == host)
        .correlate(Ticket)
    )
    stuck_tickets = list(session.scalars(
        select(Ticket).where(Ticket.status == "running", exists(host_run_subq))
    ))
    count = 0
    for t in stuck_tickets:
        local_runs = list(session.scalars(
            select(Run).where(Run.ticket_id == t.id, Run.finished_at.is_(None), Run.host == host)
        ))
        any_alive = False
        for r in local_runs:
            # Per-turn liveness check via Run.pid (a Turn is one execution within
            # a conversation). Without this, an every-tick orphan sweep would
            # kill any Run row whose subprocess the *daemon* doesn't know about
            # — for example, runs spawned by a manually-invoked
            # ``nightdesk-run-ticket`` CLI on the same host. We only mark crashed
            # when the pid is provably dead.
            if _pid_alive(r.pid):
                any_alive = True
                continue
            log.info("marking orphaned turn %s as worker_crash (ticket %s, pid %s)",
                     r.id, t.id, r.pid)
            r.finished_at = now
            r.exit_status = "worker_crash"
            r.error_summary = "worker process died before run completed"
            count += 1
            # Sync the active conversation's status/totals off the crashed turn.
            sync_conversation_from_turn(session, r)
        if any_alive:
            # Some local turn is still in flight; don't touch the ticket.
            continue
        other_in_flight = session.scalar(
            select(Run).where(Run.ticket_id == t.id, Run.finished_at.is_(None), Run.host != host)
        )
        if other_in_flight is None:
            # v2: any turn-completion lands the ticket in 'review' so the
            # user can inspect the failure and requeue.
            log.info("transitioning orphaned ticket %s from running to review", t.id)
            record_transition_event(
                session, t, from_status=t.status, to_status="review",
                actor=run_actor(t.current_run_id),
            )
            t.status = "review"
    session.commit()
    if count:
        log.info("orphan recovery pass 1: marked %d run(s) as worker_crash on host %s",
                 count, host)

    # Pass 2: tickets stuck in running with no Run row at all.
    # Wait at least RUNLESS_GRACE_SECONDS before resetting so a freshly
    # picked ticket (status='running' but the runner hasn't called
    # start_run yet) isn't ripped out from under itself. Without this
    # debounce, the every-tick sweep races with the daemon's pick
    # sequence (transition_status('running') -> spawn subproc -> subproc
    # calls start_run) and the runner's cancel watcher sees the ticket
    # bounce out of 'running' and aborts the new run as 'cancelled'.
    from datetime import timedelta
    RUNLESS_GRACE_SECONDS = 30
    cutoff = now - timedelta(seconds=RUNLESS_GRACE_SECONDS)
    no_run_subq = (
        select(Run.id)
        .where(Run.ticket_id == Ticket.id, Run.finished_at.is_(None))
        .correlate(Ticket)
    )
    runless = list(session.scalars(
        select(Ticket).where(
            Ticket.status == "running",
            Ticket.updated_at < cutoff,
            ~exists(no_run_subq),
        )
    ))
    for t in runless:
        log.info("resetting runless ticket %s from running to queued", t.id)
        record_transition_event(
            session, t, from_status=t.status, to_status="queued",
            actor=run_actor(t.current_run_id),
        )
        t.status = "queued"
        t.current_run_id = None
        # NOTE: deliberately do NOT clear current_conversation_id. Pass 2
        # clears the in-flight TURN (current_run_id), not the active
        # conversation itself — the conversation is history, revisit-able and
        # re-continuable. (With the turn row now created before workspace prep,
        # this runless branch rarely fires; it remains a safety net for tickets
        # wedged into 'running' outside the worker, e.g. a manual DB poke.)
        # Don't reset run_now — the user originally asked for it; let the
        # scheduler bypass capacity on the next tick.
    session.commit()
    if runless:
        log.info("orphan recovery pass 2: reset %d runless ticket(s) to queued on host %s",
                 len(runless), host)

    # Pass 3: mid-run steering claim recovery. A ``delivering`` SteerMessage was
    # claimed by a live-run watcher (``pending -> delivering``) but never
    # confirmed delivered. If its ticket is no longer ``running``, the run that
    # claimed it died before delivering; reset it to ``pending`` so it is
    # visible in the queue again and gets redelivered (or drained) on the next
    # turn instead of being stuck in the transient claim state forever. A
    # ``delivering`` row on a still-running ticket is a legitimate in-flight
    # claim and is left alone.
    from nightdesk.db.models import SteerMessage
    stuck_steer = list(session.scalars(
        select(SteerMessage)
        .join(Ticket, SteerMessage.ticket_id == Ticket.id)
        .where(
            SteerMessage.state == "delivering",
            SteerMessage.delivered_run_id.is_(None),
            Ticket.status != "running",
        )
    ))
    for m in stuck_steer:
        m.state = "pending"
    session.commit()
    if stuck_steer:
        log.info("orphan recovery pass 3: reset %d orphaned steer claim(s) to pending",
                 len(stuck_steer))
    return count
