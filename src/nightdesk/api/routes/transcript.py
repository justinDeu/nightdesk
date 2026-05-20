"""Transcript SSE endpoint, split out of `ui.py` so downstream UI streams
can rewrite ui.py without colliding with backend transcript work.

The endpoint streams *canonical* events. If the transcript file is the new
NDJSON format, each event is sent as ``event: <type>\\ndata: <json>\\n\\n``.
Legacy raw-SDK files fall back to one ``data: <raw line>`` per line so the
client can still display something.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.db.models import Run
from nightdesk.domain.runs import list_runs
from nightdesk.domain.tickets import get_ticket, TicketNotFound
from nightdesk.transcript import is_canonical


def _format_sse(line: str, since_seq: int = -1) -> str:
    """Format a transcript line for SSE.

    If the line parses as a canonical event with a ``type`` field, emit it as
    a typed SSE event so the client renderer can dispatch directly. Otherwise
    fall back to plain ``data:``.

    ``since_seq`` is the highest ``seq`` the client already rendered server-side
    on initial page load. Canonical events at or below that watermark are
    suppressed so the connect-time replay of the on-disk transcript does not
    re-emit events the page already shows (the duplicate "Run started" meta,
    drifting tool cards, etc.). Events without a numeric ``seq`` always pass
    through.
    """
    stripped = line.rstrip("\n")
    if not stripped:
        return ""
    try:
        evt = json.loads(stripped)
    except json.JSONDecodeError:
        return f"data: {stripped}\n\n"
    if isinstance(evt, dict) and isinstance(evt.get("type"), str):
        seq = evt.get("seq")
        if isinstance(seq, int) and seq <= since_seq:
            return ""
        return f"event: {evt['type']}\ndata: {stripped}\n\n"
    return f"data: {stripped}\n\n"


def build_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(tags=["transcript"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("/api/v1/tickets/{tid}/transcript", dependencies=[auth])
    async def transcript_sse(
        tid: str,
        since_seq: int = Query(-1),
        session: Session = Depends(get_session),
    ):
        try:
            t = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")

        path: Path | None = None
        if t.current_run_id is not None:
            r = session.get(Run, t.current_run_id)
            if r is not None and r.transcript_path:
                path = Path(r.transcript_path)
        if path is None:
            runs = list_runs(session, ticket_id=tid)
            if runs:
                path = Path(runs[0].transcript_path)

        if path is None:
            async def empty_gen():
                yield "event: end\ndata: done\n\n"
            return StreamingResponse(empty_gen(), media_type="text/event-stream")

        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("")

        canonical = is_canonical(path)

        async def gen():
            with path.open("r", encoding="utf-8") as f:
                # Send existing content first. Canonical events at or below the
                # client's server-rendered watermark are suppressed so the
                # replay does not duplicate what the page already shows.
                for line in f:
                    chunk = _format_sse(line, since_seq) if canonical else _legacy_chunk(line)
                    if chunk:
                        yield chunk
                # Then tail until the ticket leaves 'running'.
                while True:
                    line = f.readline()
                    if not line:
                        session.expire_all()
                        cur = session.get(type(t), tid)
                        if cur is None or cur.status != "running":
                            yield "event: end\ndata: done\n\n"
                            return
                        await asyncio.sleep(0.5)
                        continue
                    chunk = _format_sse(line, since_seq) if canonical else _legacy_chunk(line)
                    if chunk:
                        yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    return router


def _legacy_chunk(line: str) -> str:
    stripped = line.rstrip("\n")
    if not stripped:
        return ""
    return f"data: {stripped}\n\n"
