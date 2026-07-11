"""Resident interactive agents (UI label: "Agent") — owned-table domain.

Rewritten from the v1 ticket-façade into a real domain over the ``sessions`` /
``session_turns`` / ``pending_inputs`` tables. See
``docs/design/session-suite/resident-agents-v3.md``.

Responsibilities:
- Lifecycle CRUD (create / list / get / delete / end).
- The inbox transport: enqueue user/interrupt/answer/restart turns; the host
  claims and drains them. Routing every control message through a durable
  ``SessionTurn`` means a cold host still receives it (it re-spawns and drains).
- Human-input (needs-input) answering against the ``PendingInput`` table.
- Per-agent env: encrypt secret values with ``ProfileSecretBox``, merge over the
  host env at spawn, mask secrets for the API.
- Derived liveness (``describe_liveness``) and the effective idle timeout, both
  live-evaluated (never frozen at spawn).

The API layer is a thin pass-through; the host/reaper import the claim, usage,
and reap helpers here so all state transitions live in one module.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as OrmSession

from nightdesk.db.models import ConfigRow, PendingInput, Session, SessionTurn
from nightdesk.domain.profile_secrets import ProfileSecretBox


# Turn kinds carried on the SessionTurn inbox.
TURN_KINDS = ("user", "interrupt", "answer", "restart", "reap", "wake")
# Coarse persisted Session.status values.
SESSION_STATUSES = ("active", "idle", "ended", "crashed")


class SessionNotFound(Exception):
    """Raised when a session id does not resolve."""


class SessionBusy(Exception):
    """Raised when an operation is illegal in the session's current state
    (e.g. deleting a live agent, restarting mid-stream without force). Route
    handlers map this to HTTP 409."""


class QueueFull(Exception):
    """Raised when the queued-turn cap is exceeded. Maps to HTTP 429."""


class PendingNotOpen(Exception):
    """Raised when answering a PendingInput that is already answered/cancelled.
    Maps to HTTP 409."""


class TurnNotFound(Exception):
    """Raised when a turn id does not resolve for the given agent. HTTP 404."""


# ---------------------------------------------------------------------------
# Liveness + timeout (both live-evaluated, never frozen at spawn)
# ---------------------------------------------------------------------------
def _pid_alive(pid: Optional[int]) -> bool:
    """True if ``pid`` names a live process. ``os.kill(pid, 0)`` raises
    ``ProcessLookupError`` for a dead pid and ``PermissionError`` for a live one
    we do not own (treated as alive)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def has_open_pending(session: OrmSession, session_id: str) -> bool:
    return (session.scalar(
        select(func.count(PendingInput.id)).where(
            PendingInput.session_id == session_id,
            PendingInput.status == "pending",
        )
    ) or 0) > 0


def _has_streaming_turn(session: OrmSession, session_id: str) -> bool:
    return bool(session.scalar(
        select(func.count(SessionTurn.id)).where(
            SessionTurn.session_id == session_id,
            SessionTurn.status == "streaming",
        )
    ))


def describe_liveness(
    row: Session,
    *,
    open_pending: Optional[bool] = None,
    streaming: Optional[bool] = None,
    session: Optional[OrmSession] = None,
) -> str:
    """Derive the five(+1) UI states from the coarse persisted state + reality.

    ``alive`` / ``needs-input`` / ``warm`` / ``cold`` / ``ended`` / ``crashed``.
    Pass precomputed ``open_pending`` / ``streaming`` flags (list endpoints batch
    them) or a ``session`` to compute them. Order matters: ``ended`` is terminal
    and wins; then a human-input request (the human must act) before liveness.
    """
    if row.status == "ended":
        return "ended"
    if open_pending is None:
        open_pending = has_open_pending(session, row.id) if session is not None else False
    if streaming is None:
        streaming = _has_streaming_turn(session, row.id) if session is not None else False
    if open_pending:
        return "needs-input"
    alive = _pid_alive(row.host_pid)
    if alive and streaming:
        return "alive"
    if alive:
        return "warm"
    if row.status == "crashed":
        return "crashed"
    return "cold"


def effective_timeout(row: Session, cfg: Optional[ConfigRow]) -> int:
    """Effective idle-reap timeout in seconds: the per-agent override if set,
    else the *current* global (read live at each reap pass, never frozen)."""
    if row.idle_timeout_s is not None:
        return int(row.idle_timeout_s)
    if cfg is not None and cfg.session_idle_timeout_s is not None:
        return int(cfg.session_idle_timeout_s)
    return 300


# ---------------------------------------------------------------------------
# Env: encrypt / merge / mask
# ---------------------------------------------------------------------------
def merge_env_map(existing: Optional[dict], incoming: dict, box: ProfileSecretBox) -> dict:
    """Produce the stored env map from an incoming edit.

    Stored shape: ``{KEY: {"value": <plain|cipher>, "secret": bool}}``. Secret
    values are encrypted at rest. A secret entry with ``value is None`` means
    "keep the stored cipher" (write-only editor contract) — preserved from
    ``existing``; if there is nothing stored it is dropped. Non-secret values are
    stored in the clear. Keys absent from ``incoming`` are removed (replace map).
    """
    existing = existing or {}
    out: dict[str, dict] = {}
    for key, spec in (incoming or {}).items():
        if not isinstance(spec, dict):
            spec = {"value": spec, "secret": False}
        secret = bool(spec.get("secret"))
        value = spec.get("value")
        if secret:
            if value is None:
                prev = existing.get(key)
                if isinstance(prev, dict) and prev.get("secret") and prev.get("value"):
                    out[key] = {"value": prev["value"], "secret": True}
                # else: no stored cipher and no new value -> drop the entry.
            else:
                out[key] = {"value": box.encrypt(value), "secret": True}
        else:
            out[key] = {"value": "" if value is None else str(value), "secret": False}
    return out


def decrypt_env_for_spawn(row: Session, box: ProfileSecretBox) -> dict[str, str]:
    """Decrypt the stored env map into a plain ``{KEY: value}`` for the spawn.

    Raises ``ValueError`` (from the box) if a secret cipher is unreadable — the
    host surfaces this as a clean "re-enter env secrets" spawn failure.
    """
    out: dict[str, str] = {}
    for key, spec in (row.env or {}).items():
        if not isinstance(spec, dict):
            out[key] = str(spec)
            continue
        if spec.get("secret"):
            val = box.decrypt(spec.get("value"))
            out[key] = "" if val is None else str(val)
        else:
            out[key] = str(spec.get("value") or "")
    return out


def masked_env(row: Session) -> list[dict]:
    """API-safe env view: never returns a secret value.

    ``[{"key", "secret", "set", "value"?}]`` — ``value`` is present only for
    non-secret keys; secret keys report ``set: true`` and no value.
    """
    out: list[dict] = []
    for key, spec in (row.env or {}).items():
        if not isinstance(spec, dict):
            out.append({"key": key, "secret": False, "set": True, "value": str(spec)})
            continue
        if spec.get("secret"):
            out.append({"key": key, "secret": True, "set": bool(spec.get("value"))})
        else:
            out.append({"key": key, "secret": False, "set": True,
                        "value": str(spec.get("value") or "")})
    return out


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def _scratch_root(scratch_root: Optional[Path]) -> Path:
    if scratch_root is not None:
        return Path(scratch_root)
    from nightdesk.config import load_config
    return load_config().session_scratch_root


def get_session_row(session: OrmSession, session_id: str) -> Session:
    row = session.get(Session, session_id)
    if row is None:
        raise SessionNotFound(session_id)
    return row


def create_session(
    session: OrmSession,
    *,
    title: Optional[str] = None,
    profile_id: str,
    source_path: Optional[str] = None,
    project_id: Optional[str] = None,
    env: Optional[dict] = None,
    idle_timeout_s: Optional[int] = None,
    model: Optional[str] = None,
    scratch_root: Optional[Path] = None,
    transcript_root: Optional[Path] = None,
    box: Optional[ProfileSecretBox] = None,
) -> Session:
    """Create an idle, cold agent. ``source_path`` None -> a fresh per-agent
    scratch directory. Env secrets are encrypted if ``box`` is provided."""
    from nightdesk.db.models import _uuid

    sid = _uuid()
    if source_path:
        path = str(source_path)
    else:
        scratch = _scratch_root(scratch_root) / sid
        os.makedirs(scratch, exist_ok=True)
        path = str(scratch)

    if transcript_root is None:
        from nightdesk.config import load_config
        transcript_root = load_config().transcript_root
    transcript_path = str(Path(transcript_root) / "sessions" / f"{sid}.ndjson")

    stored_env = None
    if env:
        if box is None:
            raise ValueError("a ProfileSecretBox is required to store env")
        stored_env = merge_env_map(None, env, box)

    row = Session(
        id=sid,
        title=title or "Agent",
        profile_id=profile_id,
        project_id=project_id,
        backend="claude",
        model=model,
        status="idle",
        source_path=path,
        posture="trusted",
        last_activity_at=datetime.now(timezone.utc),
        idle_timeout_s=idle_timeout_s,
        env=stored_env,
        transcript_path=transcript_path,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def list_sessions(session: OrmSession, *, limit: int = 200) -> list[Session]:
    stmt = (
        select(Session)
        .order_by(Session.updated_at.desc(), Session.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def rename_session(session: OrmSession, session_id: str, title: str) -> Session:
    """Rename an agent. The caller (API schema) trims/validates the title."""
    row = get_session_row(session, session_id)
    row.title = title
    session.commit()
    session.refresh(row)
    return row


def delete_session(session: OrmSession, session_id: str) -> None:
    """Delete an agent. 409 while a host is live (end it first)."""
    row = get_session_row(session, session_id)
    if _pid_alive(row.host_pid):
        raise SessionBusy("agent is live; end it before deleting")
    session.delete(row)
    session.commit()


def end_session(session: OrmSession, session_id: str) -> Session:
    """Request teardown. Enqueues a shutdown-equivalent control turn if live,
    and marks the row ended. The host, on seeing status='ended', tears down and
    stops; a cold agent just becomes terminal."""
    row = get_session_row(session, session_id)
    if row.status == "ended":
        return row
    if _pid_alive(row.host_pid):
        # Cancel any queued turns and any open pending; the host observes
        # status='ended' at the next boundary and shuts the inner down.
        _cancel_queued_turns(session, session_id)
        cancel_open_pending(session, session_id)
    row.status = "ended"
    row.ended_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Inbox: enqueue / claim / drain
# ---------------------------------------------------------------------------
def _next_turn_position(session: OrmSession, session_id: str) -> int:
    hi = session.scalar(
        select(func.max(SessionTurn.position)).where(
            SessionTurn.session_id == session_id
        )
    )
    return (hi or 0) + 1


def _queued_count(session: OrmSession, session_id: str,
                  kind: Optional[str] = None) -> int:
    stmt = select(func.count(SessionTurn.id)).where(
        SessionTurn.session_id == session_id,
        SessionTurn.status == "queued",
    )
    if kind is not None:
        stmt = stmt.where(SessionTurn.kind == kind)
    return session.scalar(stmt) or 0


def enqueue_turn(
    session: OrmSession,
    session_id: str,
    *,
    kind: str,
    body: str = "",
    ref_request_id: Optional[str] = None,
    enforce_cap: bool = True,
) -> SessionTurn:
    """Append a control/user turn to the inbox and bump last_activity_at.

    ``kind`` in :data:`TURN_KINDS`. User turns are subject to the queued-turn
    cap; control turns (interrupt/answer/restart) bypass it — they must always
    land. Bumps ``last_activity_at`` so a just-messaged cold agent is not
    immediately reaped as it wakes.
    """
    if kind not in TURN_KINDS:
        raise ValueError(f"unknown turn kind {kind!r}")
    row = get_session_row(session, session_id)
    if row.status == "ended":
        raise SessionBusy("agent has ended")
    if enforce_cap and kind == "user":
        cfg = session.get(ConfigRow, 1)
        cap = int(cfg.max_queued_turns) if cfg and cfg.max_queued_turns else 20
        # Only user turns count toward the cap: a queued wake/control turn
        # (e.g. the create-time wake) must not eat message capacity.
        if _queued_count(session, session_id, kind="user") >= cap:
            raise QueueFull(f"queued-turn cap ({cap}) reached")
    turn = SessionTurn(
        session_id=session_id,
        position=_next_turn_position(session, session_id),
        kind=kind,
        body=body,
        ref_request_id=ref_request_id,
        status="queued",
    )
    session.add(turn)
    row.last_activity_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(turn)
    return turn


def post_message(session: OrmSession, session_id: str, message: str) -> SessionTurn:
    """Enqueue a user turn (the common send path)."""
    return enqueue_turn(session, session_id, kind="user", body=message)


def request_interrupt(session: OrmSession, session_id: str) -> SessionTurn:
    """Enqueue an interrupt control turn. 409 if nothing is in flight.

    In-flight = a streaming turn OR an open pending input (a parked needs-input
    turn counts — interrupt resolves it with a synthetic deny host-side).
    """
    row = get_session_row(session, session_id)
    if not _has_streaming_turn(session, session_id) and not has_open_pending(session, session_id):
        raise SessionBusy("nothing in flight to interrupt")
    return enqueue_turn(session, session_id, kind="interrupt", enforce_cap=False)


def request_restart(
    session: OrmSession,
    session_id: str,
    *,
    force: bool = False,
    clear_context: bool = False,
) -> SessionTurn:
    """Enqueue a runtime-restart control turn. 409 if streaming and not forced.

    ``clear_context`` wipes the resume handle here (not just host-side): a live
    host then respawns the inner WITHOUT ``--resume`` (fresh conversation
    context), and a cold agent's next host boots fresh too instead of resuming
    first and restarting after. The transcript ndjson is never touched — the
    log survives; only the model's conversation context resets.
    """
    row = get_session_row(session, session_id)
    if _has_streaming_turn(session, session_id) and not force:
        raise SessionBusy("a turn is streaming; pass force to interrupt and restart")
    if clear_context:
        row.resume_handle = None
        session.commit()
    return enqueue_turn(
        session, session_id, kind="restart",
        body=json.dumps({"force": bool(force), "clear_context": bool(clear_context)}),
        enforce_cap=False,
    )


def wake_session(session: OrmSession, session_id: str) -> Session:
    """Wake a cold agent without a message. The supervisor spawns hosts only
    for sessions with a queued turn (the one spawn signal), so a wake enqueues
    a no-op ``wake`` control turn when the agent has no live host and an empty
    inbox — the host claims it at boot and marks it done. Also bumps
    ``last_activity_at`` (so the fresh host is not instantly idle-reaped) and
    recovers ``crashed`` -> ``idle``. Reused by create (wake-on-create) and the
    explicit wake endpoint; the cap is respected downstream — a wake past
    ``max_live_sessions`` stays queued until a slot frees, like any work."""
    row = get_session_row(session, session_id)
    if row.status == "ended":
        raise SessionBusy("agent has ended")
    row.last_activity_at = datetime.now(timezone.utc)
    if row.status == "crashed":
        row.status = "idle"
    session.commit()
    if not _pid_alive(row.host_pid) and _queued_count(session, session_id) == 0:
        # Empty inbox and no host: create the work item that makes the
        # supervisor spawn one. Any already-queued turn triggers spawn itself.
        enqueue_turn(session, session_id, kind="wake", enforce_cap=False)
    session.refresh(row)
    return row


def _cancel_queued_turns(session: OrmSession, session_id: str) -> int:
    result = session.execute(
        update(SessionTurn)
        .where(SessionTurn.session_id == session_id, SessionTurn.status == "queued")
        .values(status="cancelled", finished_at=datetime.now(timezone.utc))
    )
    session.commit()
    return result.rowcount or 0


def next_queued_turn(session: OrmSession, session_id: str) -> Optional[SessionTurn]:
    """The oldest queued turn for a session (host inbox poll)."""
    return session.scalar(
        select(SessionTurn)
        .where(SessionTurn.session_id == session_id, SessionTurn.status == "queued")
        .order_by(SessionTurn.position.asc())
        .limit(1)
    )


def claim_turn(session: OrmSession, turn_id: str) -> Optional[SessionTurn]:
    """Atomically move a queued turn to 'delivering'. Returns the row on success,
    None if another claimant won (status was not 'queued')."""
    result = session.execute(
        update(SessionTurn)
        .where(SessionTurn.id == turn_id, SessionTurn.status == "queued")
        .values(status="delivering", started_at=datetime.now(timezone.utc))
    )
    session.commit()
    if (result.rowcount or 0) == 0:
        return None
    return session.get(SessionTurn, turn_id)


# ---------------------------------------------------------------------------
# Queue-strip operations (edit / reorder / cancel undelivered turns)
# ---------------------------------------------------------------------------
def list_queue_turns(session: OrmSession, session_id: str) -> list[SessionTurn]:
    """The live queue strip: turns not yet fully delivered (queued + delivering),
    oldest first. Mirrors the ticket steer queue's pre-delivery view."""
    get_session_row(session, session_id)
    return list(session.scalars(
        select(SessionTurn)
        .where(SessionTurn.session_id == session_id,
               SessionTurn.status.in_(("queued", "delivering")))
        .order_by(SessionTurn.position.asc())
    ))


def _get_turn(session: OrmSession, session_id: str, turn_id: str) -> SessionTurn:
    turn = session.get(SessionTurn, turn_id)
    if turn is None or turn.session_id != session_id:
        raise TurnNotFound(turn_id)
    return turn


def edit_turn_body(session: OrmSession, session_id: str, turn_id: str, body: str) -> SessionTurn:
    """Edit a queued turn's body. Queued-only (409 once it is delivering/done)."""
    turn = _get_turn(session, session_id, turn_id)
    if turn.status != "queued":
        raise SessionBusy("turn is no longer queued")
    turn.body = body
    session.commit()
    session.refresh(turn)
    return turn


def cancel_turn(session: OrmSession, session_id: str, turn_id: str) -> SessionTurn:
    """Cancel a queued turn (queued -> cancelled). 409 once it has left the queue."""
    turn = _get_turn(session, session_id, turn_id)
    if turn.status != "queued":
        raise SessionBusy("turn is no longer queued")
    turn.status = "cancelled"
    turn.finished_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(turn)
    return turn


def reorder_turns(session: OrmSession, session_id: str, ordered_ids: list[str]) -> list[SessionTurn]:
    """Reorder queued turns among themselves. Every id must be a queued turn of
    this agent (404 otherwise); positions are reassigned in the given order,
    reusing the turns' own existing position slots so already-delivering turns
    are untouched."""
    get_session_row(session, session_id)
    queued = {
        t.id: t
        for t in session.scalars(
            select(SessionTurn).where(
                SessionTurn.session_id == session_id,
                SessionTurn.status == "queued",
            )
        )
    }
    for tid in ordered_ids:
        if tid not in queued:
            raise TurnNotFound(tid)
    slots = sorted(t.position for t in queued.values())
    for slot, tid in zip(slots, ordered_ids):
        queued[tid].position = slot
    session.commit()
    return list_queue_turns(session, session_id)


def request_reap(session: OrmSession, session_id: str) -> Optional[SessionTurn]:
    """Graceful explicit reap: 409 if a turn is streaming or a pending input is
    open; otherwise ask a live host to run its normal idle-reap now (disconnect,
    publish resume_handle, status=idle) via a durable control turn. A cold agent
    is already reaped — returns None (no-op)."""
    row = get_session_row(session, session_id)
    if _has_streaming_turn(session, session_id):
        raise SessionBusy("a turn is streaming; cannot reap")
    if has_open_pending(session, session_id):
        raise SessionBusy("a human-input request is open; cannot reap")
    if not _pid_alive(row.host_pid):
        return None
    return enqueue_turn(session, session_id, kind="reap", enforce_cap=False)


# ---------------------------------------------------------------------------
# Host row claim / release
# ---------------------------------------------------------------------------
def claim_host(session: OrmSession, session_id: str, *, host: str, pid: int) -> bool:
    """Conditional claim of the Session row for a host (``WHERE host_pid IS
    NULL``). Returns True if this host won, False if another already holds it.
    Guards against double-spawn."""
    result = session.execute(
        update(Session)
        .where(Session.id == session_id, Session.host_pid.is_(None))
        .values(host=host, host_pid=pid, status="active",
                last_activity_at=datetime.now(timezone.utc))
    )
    session.commit()
    return (result.rowcount or 0) > 0


def release_host(
    session: OrmSession,
    session_id: str,
    *,
    status: str = "idle",
    resume_handle: Optional[dict] = None,
) -> None:
    """Clear host ownership on a clean reap/shutdown. Persists the resume handle
    so the next cold-start resumes the same claude session id."""
    row = session.get(Session, session_id)
    if row is None:
        return
    row.host = None
    row.host_pid = None
    if row.status != "ended":
        row.status = status
    if resume_handle is not None:
        row.resume_handle = resume_handle
    session.commit()


# ---------------------------------------------------------------------------
# Pending inputs (needs-input)
# ---------------------------------------------------------------------------
def open_pending(session: OrmSession, session_id: str) -> Optional[PendingInput]:
    return session.scalar(
        select(PendingInput)
        .where(PendingInput.session_id == session_id, PendingInput.status == "pending")
        .order_by(PendingInput.created_at.desc())
        .limit(1)
    )


def create_pending(
    session: OrmSession,
    session_id: str,
    *,
    request_id: str,
    kind: str,
    tool: Optional[str],
    payload: dict,
) -> Optional[PendingInput]:
    """Insert a PendingInput row (host-side, on a `pending_input` control event).

    The partial unique index guarantees at most one open row per agent; a
    double-emit hits an IntegrityError, which we swallow and treat as "the first
    one stands" (returns None). Also bumps last_activity_at."""
    from sqlalchemy.exc import IntegrityError

    row = PendingInput(
        session_id=session_id,
        request_id=request_id,
        kind=kind,
        tool=tool,
        payload=payload or {},
        status="pending",
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    srow = session.get(Session, session_id)
    if srow is not None:
        srow.last_activity_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(row)
    return row


def answer_pending(
    session: OrmSession,
    session_id: str,
    request_id: str,
    *,
    decision: str,
    answer: Optional[str] = None,
    updated_input: Optional[dict] = None,
) -> SessionTurn:
    """Record a human answer and enqueue the durable answer turn for the host.

    Validates the row is still ``pending`` (409 otherwise) and stores the chosen
    decision for audit. The host claims the answer turn and writes it into the
    inner's stdin, resolving the parked ``can_use_tool`` future. Marking the row
    ``answered`` happens host-side once the inner confirms (``pending_resolved``).
    """
    row = session.scalar(
        select(PendingInput).where(
            PendingInput.session_id == session_id,
            PendingInput.request_id == request_id,
        )
    )
    if row is None:
        raise PendingNotOpen(f"no such pending input {request_id!r}")
    if row.status != "pending":
        raise PendingNotOpen("pending input already resolved")
    payload = {"decision": decision}
    if answer is not None:
        payload["answer"] = answer
    if updated_input is not None:
        payload["updated_input"] = updated_input
    row.answer = payload
    session.commit()
    return enqueue_turn(
        session, session_id, kind="answer",
        body=json.dumps(payload), ref_request_id=request_id, enforce_cap=False,
    )


def resolve_pending(
    session: OrmSession,
    session_id: str,
    request_id: str,
    *,
    status: str = "answered",
) -> None:
    """Host-side: mark a pending row answered/cancelled once the inner resolves."""
    row = session.scalar(
        select(PendingInput).where(
            PendingInput.session_id == session_id,
            PendingInput.request_id == request_id,
        )
    )
    if row is None or row.status != "pending":
        return
    row.status = status
    row.answered_at = datetime.now(timezone.utc)
    session.commit()


def cancel_open_pending(session: OrmSession, session_id: str) -> int:
    """Cancel every open pending row for a session (interrupt / crash sweep)."""
    result = session.execute(
        update(PendingInput)
        .where(PendingInput.session_id == session_id, PendingInput.status == "pending")
        .values(status="cancelled", answered_at=datetime.now(timezone.utc))
    )
    session.commit()
    return result.rowcount or 0


def list_open_pending(session: OrmSession) -> list[PendingInput]:
    """Every open pending input across all agents (sidebar badge + Desk band)."""
    return list(session.scalars(
        select(PendingInput)
        .where(PendingInput.status == "pending")
        .order_by(PendingInput.created_at.asc())
    ))


# ---------------------------------------------------------------------------
# Seen / unread (attention)
# ---------------------------------------------------------------------------
# A session is UNREAD when the agent replied past what the human has seen: it
# has at least one completed user turn AND the newest such turn finished after
# ``last_seen_at`` (or the stamp is NULL). A done user turn's ``finished_at``
# is the reliable "assistant replied" instant — the host stamps it exactly
# when the turn's result lands (``session_host``'s turn-complete handler) —
# whereas ``last_activity_at`` also bumps on the human's own sends, which
# would flag a just-sent-and-walked-away message as unread before any reply.

def mark_seen(session: OrmSession, session_id: str) -> Session:
    """Stamp the seen high-water mark to now (always moves forward)."""
    row = get_session_row(session, session_id)
    row.last_seen_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(row)
    return row


def mark_unread(session: OrmSession, session_id: str) -> Session:
    """Rewind the seen stamp (NULL), re-raising attention on this agent.

    Only sessions with a completed user turn ever compute as unread, so
    clearing the stamp on a never-used agent is a no-op for the badge."""
    row = get_session_row(session, session_id)
    row.last_seen_at = None
    session.commit()
    session.refresh(row)
    return row


def is_unread(session: OrmSession, session_id: str) -> bool:
    """Unread flag for one session (list rows / seen responses)."""
    srow = session.get(Session, session_id)
    if srow is None or srow.status == "ended":
        return False
    last_reply = session.scalar(
        select(func.max(SessionTurn.finished_at)).where(
            SessionTurn.session_id == session_id,
            SessionTurn.kind == "user",
            SessionTurn.status == "done",
        )
    )
    if last_reply is None:
        return False
    return srow.last_seen_at is None or last_reply > srow.last_seen_at


def list_unread(session: OrmSession) -> list[tuple[Session, datetime]]:
    """All unread sessions with their last-reply instant, newest reply first.

    One grouped query (no per-session round trips); the datetime comparison
    happens in SQL, which is safe because every writer stamps aware-UTC
    datetimes, so the stored values compare consistently."""
    sub = (
        select(
            SessionTurn.session_id.label("session_id"),
            func.max(SessionTurn.finished_at).label("last_reply_at"),
        )
        .where(SessionTurn.kind == "user", SessionTurn.status == "done")
        .group_by(SessionTurn.session_id)
        .subquery()
    )
    rows = session.execute(
        select(Session, sub.c.last_reply_at)
        .join(sub, sub.c.session_id == Session.id)
        .where(
            Session.status != "ended",
            sub.c.last_reply_at.isnot(None),
            (Session.last_seen_at.is_(None))
            | (sub.c.last_reply_at > Session.last_seen_at),
        )
        .order_by(sub.c.last_reply_at.desc())
    )
    return [(srow, last_reply) for srow, last_reply in rows]


def last_assistant_preview(row: Session, *, max_chars: int = 160) -> Optional[str]:
    """Last assistant_text snippet from the transcript tail, or None.

    Cheap by construction: reads at most the final 32 KiB of the ndjson and
    scans backwards. Any I/O or parse trouble degrades to no preview."""
    if not row.transcript_path:
        return None
    try:
        with open(row.transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 32768))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except (ValueError, TypeError):
            continue
        if evt.get("type") == "assistant_text" and isinstance(evt.get("text"), str):
            text = " ".join(evt["text"].split())
            if text:
                return text[:max_chars]
    return None


# ---------------------------------------------------------------------------
# Env replace (API)
# ---------------------------------------------------------------------------
def nudge_worker(session: OrmSession) -> bool:
    """Best-effort SIGUSR1 to the live worker so it breaks its tick sleep and
    spawns a host now (cold-start latency optimization; never load-bearing —
    the next plain tick spawns it anyway). Returns True if a signal was sent.
    """
    import signal as _signal
    from nightdesk.db.models import WorkerHeartbeat

    hb = session.get(WorkerHeartbeat, 1)
    if hb is None or not hb.pid:
        return False
    try:
        os.kill(hb.pid, _signal.SIGUSR1)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def put_env(session: OrmSession, session_id: str, env: dict, box: ProfileSecretBox) -> Session:
    """Replace the agent's env map (no restart). Encrypts secret values,
    preserves untouched ciphers (write-only secret contract)."""
    row = get_session_row(session, session_id)
    row.env = merge_env_map(row.env, env or {}, box)
    session.commit()
    session.refresh(row)
    return row
