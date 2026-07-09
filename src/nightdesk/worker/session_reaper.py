"""Supervisor-side decisions for resident agents: spawn, orphan sweep, LRU cap.

Pure-ish decision functions the worker's supervisor pass calls each tick. Idle
self-reap lives in the host (it owns the inner); this module handles what the
host cannot decide alone: which cold agents need a host, which live agents to
evict when over the ``max_live_sessions`` cap, and re-adopting/failing agents
whose host process died.

Everything reads the effective timeout / cap LIVE from the current ConfigRow
(never frozen), so a Settings change takes effect on the next pass.
"""
from __future__ import annotations

import logging
import os
import signal
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from nightdesk.db.models import ConfigRow, PendingInput, Session, SessionTurn
from nightdesk.domain import sessions as sess
from nightdesk.transcript import append_event


log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sessions_needing_host(db: OrmSession) -> list[Session]:
    """Cold agents that have queued work (a queued turn) and no live host.

    A wake with an empty inbox is intentionally NOT spawned here — ``wake`` only
    bumps ``last_activity_at``; the message enqueue is what creates work.
    """
    rows = db.scalars(
        select(Session).where(Session.status.in_(("idle", "active", "crashed")))
    ).all()
    out: list[Session] = []
    for row in rows:
        if sess._pid_alive(row.host_pid):
            continue
        queued = db.scalar(
            select(SessionTurn.id).where(
                SessionTurn.session_id == row.id,
                SessionTurn.status == "queued",
            ).limit(1)
        )
        if queued is not None:
            out.append(row)
    return out


def live_sessions(db: OrmSession) -> list[Session]:
    rows = db.scalars(
        select(Session).where(Session.status.in_(("active",)))
    ).all()
    return [r for r in rows if sess._pid_alive(r.host_pid)]


def over_cap_evictions(db: OrmSession) -> list[Session]:
    """Live agents to evict (SIGTERM their host) to get back under the cap.

    LRU by ``last_activity_at``. Agents with an open PendingInput or a streaming
    turn are pinned (never evicted — the human/turn is the blocker).
    """
    cfg = db.get(ConfigRow, 1)
    cap = int(cfg.max_live_sessions) if cfg and cfg.max_live_sessions else 4
    live = live_sessions(db)
    if len(live) <= cap:
        return []
    evictable = [
        r for r in live
        if not sess.has_open_pending(db, r.id) and not _has_streaming(db, r.id)
    ]
    evictable.sort(key=lambda r: _as_utc(r.last_activity_at))
    n_to_evict = len(live) - cap
    return evictable[:n_to_evict]


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _has_streaming(db: OrmSession, session_id: str) -> bool:
    return bool(db.scalar(
        select(SessionTurn.id).where(
            SessionTurn.session_id == session_id,
            SessionTurn.status == "streaming",
        ).limit(1)
    ))


def evict(session: Session) -> bool:
    """SIGTERM a live host so it reaps gracefully (to idle, resume armed)."""
    if not session.host_pid:
        return False
    try:
        os.kill(session.host_pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def orphan_sweep(db: OrmSession, *, host: str) -> int:
    """Re-adopt/fail agents whose host process died uncleanly.

    An agent marked ``active`` with a dead ``host_pid`` crashed: fail its
    streaming turn, cancel its open pending rows, drop a ``session_crashed``
    breadcrumb, and land it ``idle`` (resume armed) so the next message
    cold-starts. Returns the number of agents swept.
    """
    rows = db.scalars(
        select(Session).where(Session.status == "active")
    ).all()
    swept = 0
    for row in rows:
        if sess._pid_alive(row.host_pid):
            continue
        swept += 1
        # Fail any streaming turn.
        for turn in db.scalars(
            select(SessionTurn).where(
                SessionTurn.session_id == row.id,
                SessionTurn.status.in_(("streaming", "delivering")),
            )
        ).all():
            turn.status = "failed"
            turn.error = "host crashed mid-turn"
            turn.finished_at = _now()
        # Cancel open pending (the parked callback is gone).
        for p in db.scalars(
            select(PendingInput).where(
                PendingInput.session_id == row.id,
                PendingInput.status == "pending",
            )
        ).all():
            p.status = "cancelled"
            p.answered_at = _now()
        row.host = None
        row.host_pid = None
        row.status = "idle"
        db.commit()
        try:
            append_event(row.transcript_path, {"type": "session_crashed"})
        except Exception:  # noqa: BLE001
            log.exception("could not write session_crashed breadcrumb for %s", row.id)
        log.warning("swept crashed agent host for session %s", row.id)
    return swept
