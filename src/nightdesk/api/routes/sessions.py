"""Interactive sessions (ticketless chat) — JSON API front door.

A session is a ``Ticket`` with ``kind='session'`` (see ``domain/sessions.py``).
This router is a thin delegation layer over that façade: create, list, fetch
(with conversations), post a chat turn, promote to a real ticket, archive, and
delete. Transcript/runs SSE are reused as-is — ``/api/v1/tickets/{id}/transcript``
resolves by id regardless of ``kind`` — so no streaming code lives here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.api.schemas import (
    SessionConversationOut,
    SessionCreate,
    SessionMessage,
    SessionOut,
    SessionPromote,
    TicketOut,
)
from nightdesk.domain.conversations import list_conversations
from nightdesk.domain.sessions import (
    SessionBusy,
    archive_session,
    create_session,
    list_sessions,
    post_session_turn,
    promote_session,
)
from nightdesk.domain.tickets import (
    ConversationNotResumable,
    InvalidTransition,
    TicketNotFound,
    delete_ticket,
)


def _session_to_out(session: Session, t) -> SessionOut:
    from nightdesk.api.routes.tickets import _ticket_to_out

    convs = [
        SessionConversationOut(
            id=c.id,
            backend=c.backend,
            status=c.status,
            session_id=c.session_id,
            position=c.position,
            started_at=c.started_at,
            finished_at=c.finished_at,
            cost_usd=c.cost_usd,
            input_tokens=c.input_tokens,
            output_tokens=c.output_tokens,
        )
        for c in list_conversations(session, t.id)
    ]
    return SessionOut(**_ticket_to_out(t).model_dump(), conversations=convs)


def build_router(
    get_session, bearer_token: str, *, worktree_root: Optional[Path] = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/sessions",
        tags=["sessions"],
        dependencies=[Depends(require_token_cookie_or_bearer(bearer_token))],
    )
    # Scratch workspaces live as a sibling of worktree_root so the sandbox can
    # bind-mount them (anything under ~/.local/share/nightdesk is refused).
    scratch_root = worktree_root.parent / "nightdesk-sessions" if worktree_root else None

    @router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
    async def create(payload: SessionCreate, session: Session = Depends(get_session)):
        try:
            t = create_session(
                session,
                title=payload.title or "Session",
                profile_id=payload.profile_id,
                project_id=payload.project_id,
                workspace=payload.source_path,
                scratch_root=scratch_root,
            )
        except (InvalidTransition, ValueError) as e:
            raise HTTPException(422, str(e))
        return _session_to_out(session, t)

    @router.get("", response_model=list[SessionOut])
    async def lst(session: Session = Depends(get_session)):
        return [_session_to_out(session, t) for t in list_sessions(session)]

    @router.get("/{sid}", response_model=SessionOut)
    async def get_one(sid: str, session: Session = Depends(get_session)):
        try:
            from nightdesk.domain.sessions import _get_session_ticket

            t = _get_session_ticket(session, sid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        return _session_to_out(session, t)

    @router.post("/{sid}/messages", response_model=SessionOut)
    async def post_message(
        sid: str, payload: SessionMessage, session: Session = Depends(get_session),
    ):
        try:
            t = post_session_turn(session, sid, payload.message)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except (SessionBusy, ConversationNotResumable) as e:
            raise HTTPException(409, str(e))
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return _session_to_out(session, t)

    @router.post("/{sid}/promote", response_model=TicketOut)
    async def promote(
        sid: str, payload: SessionPromote, session: Session = Depends(get_session),
    ):
        from nightdesk.api.routes.tickets import _ticket_to_out

        try:
            t = promote_session(
                session, sid,
                title=payload.title,
                prompt=payload.prompt,
                target_status=payload.target_status,
            )
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return _ticket_to_out(t)

    @router.post("/{sid}/archive", response_model=SessionOut)
    async def archive_one(sid: str, session: Session = Depends(get_session)):
        try:
            t = archive_session(session, sid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return _session_to_out(session, t)

    @router.delete("/{sid}", status_code=status.HTTP_204_NO_CONTENT)
    async def remove(sid: str, session: Session = Depends(get_session)):
        try:
            from nightdesk.domain.sessions import _get_session_ticket

            _get_session_ticket(session, sid)
            delete_ticket(session, sid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return None

    return router
