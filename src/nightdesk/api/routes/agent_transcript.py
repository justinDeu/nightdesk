"""Agent transcript SSE endpoint.

Reuses ``routes.transcript._format_sse`` + the Last-Event-ID watermark verbatim.
The only difference from the ticket stream is the tail predicate: the tail
stays open for as long as the session row exists and is not ``ended`` — an
idle (parked) agent can wake at any moment, and an open agent screen must see
the wake turn's user_message/assistant_text land live rather than holding a
dead stream. ``event: end`` is emitted only when the session is deleted or
its status becomes ``ended``. To keep idle tails cheap the poll interval
backs off: 0.5s while the agent is active (live host / queued / streaming /
pending input), 2s while it is parked.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from nightdesk.domain import scopes as sc
from nightdesk.api.routes.transcript import _format_sse
from nightdesk.db.models import Session as SessionModel
from nightdesk.domain import sessions as sess
from nightdesk.transcript import is_canonical


def _session_over(db: Session, aid: str) -> bool:
    """True once the stream should end: session row gone or status ``ended``."""
    row = db.get(SessionModel, aid)
    return row is None or row.status == "ended"


def _agent_still_active(db: Session, aid: str) -> bool:
    """True while the agent is doing (or about to do) work. Drives the poll
    backoff only — an inactive-but-alive session keeps its tail open."""
    row = db.get(SessionModel, aid)
    if row is None:
        return False
    if row.status == "ended":
        return False
    if sess._pid_alive(row.host_pid):
        return True
    if sess.has_open_pending(db, aid):
        return True
    return sess._queued_count(db, aid) > 0 or sess._has_streaming_turn(db, aid)


def build_router(get_session, bearer_token: str, scoped) -> APIRouter:
    router = APIRouter(tags=["agents"])
    # Admin-only, same posture as the agents router (seam A).
    auth = Depends(scoped(sc.AGENTS_ADMIN))

    @router.get("/api/v1/agents/{aid}/transcript", dependencies=[auth])
    async def agent_transcript_sse(
        request: Request,
        aid: str,
        since_seq: int = Query(-1),
        session: Session = Depends(get_session),
    ):
        try:
            row = sess.get_session_row(session, aid)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")

        last_event_id = request.headers.get("last-event-id")
        if last_event_id:
            try:
                since_seq = max(since_seq, int(last_event_id))
            except (TypeError, ValueError):
                pass

        path = Path(row.transcript_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("")
        canonical = is_canonical(path)

        import asyncio

        async def gen():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    chunk = _format_sse(line, since_seq) if canonical else _legacy(line)
                    if chunk:
                        yield chunk
                while True:
                    line = f.readline()
                    if not line:
                        session.expire_all()
                        if _session_over(session, aid):
                            yield "event: end\ndata: done\n\n"
                            return
                        # Idle agents keep the tail open (they can wake any
                        # moment) but poll less often.
                        active = _agent_still_active(session, aid)
                        await asyncio.sleep(0.5 if active else 2.0)
                        continue
                    chunk = _format_sse(line, since_seq) if canonical else _legacy(line)
                    if chunk:
                        yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    return router


def _legacy(line: str) -> str:
    stripped = line.rstrip("\n")
    return f"data: {stripped}\n\n" if stripped else ""
