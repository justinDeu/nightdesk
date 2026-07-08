"""Line-anchored review comments on run diffs.

Admin-authed (Phase 1). Two path families on one router:

- ``/api/v1/runs/{rid}/comments`` — list / create / request-changes for a run.
- ``/api/v1/diff-comments/{cid}`` — edit / resolve / unresolve / delete a comment.

``outdated`` is computed here, not stored: a root's ``anchor_head_sha`` is
compared to the live diff's ``head_sha`` (recomputed from git on read). On a
mismatch the client renders the thread against its captured ``anchor_text``
rather than trusting the now-shifted line number.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.api.routes.runs import _run_workspaces
from nightdesk.api.schemas import DiffCommentCreate, DiffCommentEdit, DiffCommentOut
from nightdesk.db.models import DiffComment
from nightdesk.domain.diff import compute_workspace_diff, select_diff_workspace
from nightdesk.domain.diff_comments import (
    Anchor, Author, DiffCommentNotFound, InvalidThreadOperation,
    create_comment, delete_comment, edit_comment, list_run_comments,
    reply_comment, request_changes, set_resolved,
)
from nightdesk.domain.runs import RunNotFound, get_run


def _live_head_sha(session: Session, run) -> str | None:
    """Head sha of the run's live diff, or None if it can't be computed.

    Used only to flag stale anchors — a failure to compute means "don't claim
    anything is outdated", so None is a safe default.
    """
    try:
        ws = select_diff_workspace(_run_workspaces(session, run))
    except Exception:
        return None
    if ws is None:
        return None
    result = compute_workspace_diff(
        ws, transcript_root=Path(run.transcript_path).parent, run_id=run.id,
    )
    if result is None or result.error:
        return None
    return result.head_sha or None


def _to_out(c: DiffComment, live_head_sha: str | None) -> DiffCommentOut:
    out = DiffCommentOut.model_validate(c)
    out.outdated = bool(
        c.parent_id is None
        and c.anchor_head_sha
        and live_head_sha
        and c.anchor_head_sha != live_head_sha
    )
    return out


def build_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["review-comments"],
        dependencies=[Depends(require_token_cookie_or_bearer(bearer_token))],
    )

    @router.get("/runs/{rid}/comments", response_model=list[DiffCommentOut])
    async def list_comments(rid: str, session: Session = Depends(get_session)):
        try:
            run = get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "not found")
        live = _live_head_sha(session, run)
        return [_to_out(c, live) for c in list_run_comments(session, rid)]

    @router.post("/runs/{rid}/comments", response_model=DiffCommentOut, status_code=201)
    async def create(rid: str, payload: DiffCommentCreate,
                     session: Session = Depends(get_session)):
        author = Author(kind="admin", run_id=None)
        try:
            if payload.parent_id:
                c = reply_comment(session, payload.parent_id,
                                  body=payload.body, author=author)
            else:
                get_run(session, rid)  # 404 on unknown run
                anchor = Anchor(
                    file_path=payload.file_path or "",
                    side=payload.side or "new",
                    line=payload.line,
                    anchor_head_sha=payload.anchor_head_sha,
                    anchor_text=payload.anchor_text,
                )
                c = create_comment(session, rid, anchor=anchor,
                                   body=payload.body, author=author)
        except RunNotFound:
            raise HTTPException(404, "not found")
        except DiffCommentNotFound:
            raise HTTPException(404, "not found")
        except InvalidThreadOperation as e:
            raise HTTPException(422, str(e))
        live = _live_head_sha(session, get_run(session, rid))
        return _to_out(c, live)

    @router.patch("/diff-comments/{cid}", response_model=DiffCommentOut)
    async def edit(cid: str, payload: DiffCommentEdit,
                   session: Session = Depends(get_session)):
        try:
            c = edit_comment(session, cid, payload.body)
        except DiffCommentNotFound:
            raise HTTPException(404, "not found")
        except InvalidThreadOperation as e:
            raise HTTPException(422, str(e))
        return _to_out(c, _live_head_sha(session, get_run(session, c.run_id)))

    @router.post("/diff-comments/{cid}/resolve", response_model=DiffCommentOut)
    async def resolve(cid: str, session: Session = Depends(get_session)):
        return _set_resolved(session, cid, True)

    @router.post("/diff-comments/{cid}/unresolve", response_model=DiffCommentOut)
    async def unresolve(cid: str, session: Session = Depends(get_session)):
        return _set_resolved(session, cid, False)

    def _set_resolved(session: Session, cid: str, value: bool) -> DiffCommentOut:
        try:
            c = set_resolved(session, cid, value, Author(kind="admin"))
        except DiffCommentNotFound:
            raise HTTPException(404, "not found")
        except InvalidThreadOperation as e:
            raise HTTPException(422, str(e))
        return _to_out(c, _live_head_sha(session, get_run(session, c.run_id)))

    @router.delete("/diff-comments/{cid}", status_code=204)
    async def remove(cid: str, session: Session = Depends(get_session)):
        try:
            delete_comment(session, cid)
        except DiffCommentNotFound:
            raise HTTPException(404, "not found")

    @router.post("/runs/{rid}/comments/request-changes")
    async def request_changes_route(rid: str, session: Session = Depends(get_session)):
        try:
            get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "not found")
        try:
            ticket = request_changes(session, rid)
        except InvalidThreadOperation as e:
            raise HTTPException(422, str(e))
        return {"ticket_id": ticket.id, "next_run_context": ticket.next_run_context}

    return router
