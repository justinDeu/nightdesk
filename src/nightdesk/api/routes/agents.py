"""Resident interactive agents (UI label: "Agents") — JSON API front door.

A thin pass-through over ``domain.sessions`` (the ``sessions`` /
``session_turns`` / ``pending_inputs`` tables). The API is a pure DB writer:
every control message (message / interrupt / answer / restart) is enqueued as a
durable ``SessionTurn`` the host claims — so a cold host still receives it. See
``docs/design/session-suite/resident-agents-v3.md`` §11.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from nightdesk.domain import scopes as sc
from nightdesk.api.schemas import (
    AgentAnswer,
    AgentAttentionOut,
    AgentCreate,
    AgentDetailOut,
    AgentEnvEntryOut,
    AgentEnvPut,
    AgentMessage,
    AgentOut,
    AgentPendingItem,
    AgentPendingOut,
    AgentRestart,
    AgentTurnEdit,
    AgentTurnOut,
    AgentTurnReorder,
    AgentUnreadItem,
    AgentUpdate,
)
from nightdesk.domain import sessions as sess
from nightdesk.domain.profile_secrets import ProfileSecretBox


def _to_out(db: Session, row) -> AgentOut:
    open_pending = sess.has_open_pending(db, row.id)
    liveness = sess.describe_liveness(row, open_pending=open_pending, session=db)
    return AgentOut(
        id=row.id, title=row.title, profile_id=row.profile_id,
        project_id=row.project_id, backend=row.backend, model=row.model,
        status=row.status, liveness=liveness, has_pending=open_pending,
        unread=sess.is_unread(db, row.id),
        source_path=row.source_path, posture=row.posture,
        workspace_access=row.workspace_access, idle_timeout_s=row.idle_timeout_s,
        cost_usd=row.cost_usd, input_tokens=row.input_tokens,
        output_tokens=row.output_tokens, cache_read_tokens=row.cache_read_tokens,
        cache_write_tokens=row.cache_write_tokens, created_at=row.created_at,
        updated_at=row.updated_at, ended_at=row.ended_at,
    )


def _pending_items(db: Session) -> list[AgentPendingItem]:
    """Open pending inputs across all agents, decorated with agent titles.
    Shared by /pending (legacy callers) and /attention."""
    from nightdesk.db.models import Session as SessionModel

    out = []
    for p in sess.list_open_pending(db):
        srow = db.get(SessionModel, p.session_id)
        item = AgentPendingItem.model_validate(p)
        item.session_title = srow.title if srow is not None else "Agent"
        out.append(item)
    return out


def _to_detail(db: Session, row) -> AgentDetailOut:
    base = _to_out(db, row)
    pending = sess.open_pending(db, row.id)
    turns = sorted(row.turns, key=lambda t: t.position)
    return AgentDetailOut(
        **base.model_dump(),
        turns=[AgentTurnOut.model_validate(t) for t in turns],
        pending_input=AgentPendingOut.model_validate(pending) if pending else None,
        env=[AgentEnvEntryOut(**e) for e in sess.masked_env(row)],
        claude_session_id=(row.resume_handle or {}).get("session_id"),
    )


def build_router(
    get_session, bearer_token: str, scoped, *, worktree_root: Optional[Path] = None,
) -> APIRouter:
    # Whole surface is admin-only: driving a resident agent is a human act
    # (agents.message / agents.admin are HUMAN_ONLY in the taxonomy). Gating on
    # a human-only scope routes ndk_/ndr_ tokens to a self-diagnosing 403 rather
    # than the bare 401 the legacy cookie/bearer gate would give.
    router = APIRouter(
        prefix="/api/v1/agents",
        tags=["agents"],
        dependencies=[Depends(scoped(sc.AGENTS_ADMIN))],
    )
    box = ProfileSecretBox(bearer_token) if bearer_token else None
    scratch_root = worktree_root.parent / "nightdesk-sessions" if worktree_root else None

    def _box() -> ProfileSecretBox:
        if box is None:
            raise HTTPException(500, "server has no bearer token; cannot handle secrets")
        return box

    @router.post("", response_model=AgentDetailOut, status_code=status.HTTP_201_CREATED)
    async def create(payload: AgentCreate, session: Session = Depends(get_session)):
        try:
            row = sess.create_session(
                session,
                title=payload.title,
                profile_id=payload.profile_id,
                source_path=payload.source_path,
                project_id=payload.project_id,
                env=payload.env,
                idle_timeout_s=payload.idle_timeout_s,
                model=payload.model,
                scratch_root=scratch_root,
                box=box,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        if payload.wake:
            # Wake-on-create: reuse the wake endpoint's mechanism (a queued
            # no-op wake turn) so the next supervision tick spawns the host
            # instead of the agent sitting cold until its first message. Cap
            # semantics are the supervisor's: over max_live_sessions the wake
            # stays queued until a slot frees.
            sess.wake_session(session, row.id)
            sess.nudge_worker(session)
        return _to_detail(session, row)

    @router.get("", response_model=list[AgentOut])
    async def lst(session: Session = Depends(get_session)):
        return [_to_out(session, r) for r in sess.list_sessions(session)]

    @router.get("/pending", response_model=list[AgentPendingItem])
    async def pending(session: Session = Depends(get_session)):
        return _pending_items(session)

    @router.get("/attention", response_model=AgentAttentionOut)
    async def attention(session: Session = Depends(get_session)):
        """Everything agent-side that wants the human: structured needs-input
        plus unread replies. One poll feeds the nav badge and the Desk band."""
        pending = _pending_items(session)
        unread = [
            AgentUnreadItem(
                session_id=row.id,
                session_title=row.title,
                last_activity_at=row.last_activity_at,
                preview=sess.last_assistant_preview(row),
            )
            for row, _reply_at in sess.list_unread(session)
        ]
        return AgentAttentionOut(
            pending=pending, unread=unread, total=len(pending) + len(unread),
        )

    @router.get("/{aid}", response_model=AgentDetailOut)
    async def get_one(aid: str, session: Session = Depends(get_session)):
        try:
            row = sess.get_session_row(session, aid)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        return _to_detail(session, row)

    @router.patch("/{aid}", response_model=AgentOut)
    async def rename(aid: str, payload: AgentUpdate,
                     session: Session = Depends(get_session)):
        """Rename an agent (the only mutable field for now is ``title``)."""
        try:
            row = sess.rename_session(session, aid, payload.title)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        return _to_out(session, row)

    @router.post("/{aid}/seen", response_model=AgentOut)
    async def mark_seen(aid: str, session: Session = Depends(get_session)):
        """Stamp the seen high-water mark to now. The agent screen calls this
        on mount and when a streaming turn completes while visible, so an
        actively-viewed conversation never flaps unread."""
        try:
            row = sess.mark_seen(session, aid)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        return _to_out(session, row)

    @router.delete("/{aid}/seen", response_model=AgentOut)
    async def mark_unread(aid: str, session: Session = Depends(get_session)):
        """Mark-unread: rewind the seen stamp so the agent re-raises attention
        (nav badge + Desk band), like marking a text/email unread. A session
        with no completed user turn stays read — clearing the stamp on a fresh
        agent does not invent attention."""
        try:
            row = sess.mark_unread(session, aid)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        return _to_out(session, row)

    @router.delete("/{aid}", status_code=status.HTTP_204_NO_CONTENT)
    async def remove(aid: str, session: Session = Depends(get_session)):
        try:
            sess.delete_session(session, aid)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        except sess.SessionBusy as e:
            raise HTTPException(409, str(e))
        return None

    @router.post("/{aid}/messages", status_code=status.HTTP_202_ACCEPTED,
                 response_model=AgentTurnOut)
    async def post_message(aid: str, payload: AgentMessage,
                           session: Session = Depends(get_session)):
        try:
            turn = sess.post_message(session, aid, payload.message)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        except sess.QueueFull as e:
            raise HTTPException(429, str(e))
        except sess.SessionBusy as e:
            raise HTTPException(409, str(e))
        sess.nudge_worker(session)
        return AgentTurnOut.model_validate(turn)

    @router.post("/{aid}/interrupt", status_code=status.HTTP_202_ACCEPTED,
                 response_model=AgentTurnOut)
    async def interrupt(aid: str, session: Session = Depends(get_session)):
        try:
            turn = sess.request_interrupt(session, aid)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        except sess.SessionBusy as e:
            raise HTTPException(409, str(e))
        return AgentTurnOut.model_validate(turn)

    @router.post("/{aid}/end", response_model=AgentOut)
    async def end(aid: str, session: Session = Depends(get_session)):
        try:
            row = sess.end_session(session, aid)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        return _to_out(session, row)

    @router.post("/{aid}/wake", status_code=status.HTTP_202_ACCEPTED,
                 response_model=AgentOut)
    async def wake(aid: str, session: Session = Depends(get_session)):
        try:
            row = sess.wake_session(session, aid)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        except sess.SessionBusy as e:
            raise HTTPException(409, str(e))
        sess.nudge_worker(session)
        return _to_out(session, row)

    @router.post("/{aid}/pending/{request_id}", status_code=status.HTTP_202_ACCEPTED,
                 response_model=AgentTurnOut)
    async def answer(aid: str, request_id: str, payload: AgentAnswer,
                     session: Session = Depends(get_session)):
        try:
            turn = sess.answer_pending(
                session, aid, request_id,
                decision=payload.decision, answer=payload.answer,
                updated_input=payload.updated_input,
            )
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        except sess.PendingNotOpen as e:
            raise HTTPException(409, str(e))
        sess.nudge_worker(session)
        return AgentTurnOut.model_validate(turn)

    @router.put("/{aid}/env", response_model=AgentDetailOut)
    async def put_env(aid: str, payload: AgentEnvPut,
                      session: Session = Depends(get_session)):
        try:
            row = sess.put_env(session, aid, payload.env, _box())
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        return _to_detail(session, row)

    @router.post("/{aid}/restart-runtime", status_code=status.HTTP_202_ACCEPTED,
                 response_model=AgentTurnOut)
    async def restart_runtime(aid: str, payload: AgentRestart,
                              session: Session = Depends(get_session)):
        try:
            turn = sess.request_restart(session, aid, force=payload.force,
                                        clear_context=payload.clear_context)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        except sess.SessionBusy as e:
            raise HTTPException(409, str(e))
        sess.nudge_worker(session)
        return AgentTurnOut.model_validate(turn)

    @router.post("/{aid}/reap", status_code=status.HTTP_202_ACCEPTED,
                 response_model=AgentOut)
    async def reap(aid: str, session: Session = Depends(get_session)):
        try:
            sess.request_reap(session, aid)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        except sess.SessionBusy as e:
            raise HTTPException(409, str(e))
        sess.nudge_worker(session)
        return _to_out(session, sess.get_session_row(session, aid))

    # --- queue strip (undelivered turns) -----------------------------------
    @router.get("/{aid}/turns", response_model=list[AgentTurnOut])
    async def list_turns(aid: str, session: Session = Depends(get_session)):
        try:
            turns = sess.list_queue_turns(session, aid)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        return [AgentTurnOut.model_validate(t) for t in turns]

    @router.post("/{aid}/turns/reorder", response_model=list[AgentTurnOut])
    async def reorder_turns(aid: str, payload: AgentTurnReorder,
                            session: Session = Depends(get_session)):
        try:
            turns = sess.reorder_turns(session, aid, payload.ordered_ids)
        except sess.SessionNotFound:
            raise HTTPException(404, "not found")
        except sess.TurnNotFound as e:
            raise HTTPException(404, f"turn not queued: {e}")
        return [AgentTurnOut.model_validate(t) for t in turns]

    @router.patch("/{aid}/turns/{tid}", response_model=AgentTurnOut)
    async def edit_turn(aid: str, tid: str, payload: AgentTurnEdit,
                        session: Session = Depends(get_session)):
        try:
            turn = sess.edit_turn_body(session, aid, tid, payload.body)
        except sess.TurnNotFound:
            raise HTTPException(404, "not found")
        except sess.SessionBusy as e:
            raise HTTPException(409, str(e))
        return AgentTurnOut.model_validate(turn)

    @router.delete("/{aid}/turns/{tid}", response_model=AgentTurnOut)
    async def cancel_turn(aid: str, tid: str, session: Session = Depends(get_session)):
        try:
            turn = sess.cancel_turn(session, aid, tid)
        except sess.TurnNotFound:
            raise HTTPException(404, "not found")
        except sess.SessionBusy as e:
            raise HTTPException(409, str(e))
        return AgentTurnOut.model_validate(turn)

    return router
