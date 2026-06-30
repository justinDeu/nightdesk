from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from pathlib import Path

from nightdesk.api.auth import require_bearer
from nightdesk.api.schemas import RunOut
from nightdesk.db.models import TicketWorkspace
from nightdesk.domain.diff import (
    RunDiff, compute_workspace_diff, select_diff_workspace,
)
from nightdesk.domain.runs import get_run, list_runs, RunNotFound
from nightdesk.logging_setup import run_log_path


def build_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/runs",
        tags=["runs"],
        dependencies=[Depends(require_bearer(bearer_token))],
    )

    @router.get("", response_model=list[RunOut])
    async def lst(ticket_id: str | None = Query(default=None),
                   session: Session = Depends(get_session)):
        return list_runs(session, ticket_id=ticket_id)

    @router.get("/{rid}", response_model=RunOut)
    async def show(rid: str, session: Session = Depends(get_session)):
        try:
            return get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "not found")

    @router.get("/{rid}/diff")
    async def run_diff(rid: str, session: Session = Depends(get_session)):
        """Return a structured unified diff for the run's workspace changes.

        Selects the run's diffable workspace by kind and dispatches: git
        workspaces diff ``start_sha..end`` via git; non-git (directory)
        workspaces diff the run-start filesystem snapshot against the current
        tree. Both render through the same RunDiff structure.
        """
        try:
            run = get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "not found")

        ws = select_diff_workspace(_run_workspaces(session, run))
        result = compute_workspace_diff(
            ws,
            transcript_root=Path(run.transcript_path).parent,
            run_id=rid,
        )
        if result is None:
            return JSONResponse({
                "files": [],
                "total_added": 0,
                "total_deleted": 0,
                "total_files": 0,
                "truncated": False,
                "hidden_files": 0,
                "hidden_lines": 0,
                "error": "no workspace found for this run",
                "branch": "",
                "base_sha": "",
                "head_sha": "",
                "repo_root": "",
            })

        return JSONResponse(_diff_to_json(result))

    @router.get("/{rid}/log")
    async def download_log(rid: str, session: Session = Depends(get_session)):
        """Download the per-run worker log file as plain text."""
        try:
            get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "not found")
        path = run_log_path(rid)
        if not path.exists():
            return PlainTextResponse("(no log for this run)", status_code=404)
        return FileResponse(
            path,
            media_type="text/plain; charset=utf-8",
            filename=f"nightdesk-run-{rid}.log",
        )

    return router


def _run_workspaces(session: Session, run) -> list:
    """Candidate workspaces for a run's diff, conversation-scoped first.

    A Conversation owns its workspaces (1:N), so the diff for any turn in it is
    computed against the tree THAT conversation ran against. Preference order:
    the run's conversation's workspaces; then run-scoped rows (legacy); then the
    ticket's workspaces (older legacy rows). The caller picks the diffable one
    via ``select_diff_workspace``. Mirrors the HTMX ticket-page path.
    """
    conversation_id = getattr(run, "conversation_id", None)
    if conversation_id:
        conv_scoped = list(session.execute(
            select(TicketWorkspace)
            .where(TicketWorkspace.conversation_id == conversation_id)
            .order_by(TicketWorkspace.position)
        ).scalars())
        if conv_scoped:
            return conv_scoped
    run_scoped = list(session.execute(
        select(TicketWorkspace)
        .where(TicketWorkspace.run_id == run.id)
        .order_by(TicketWorkspace.position)
    ).scalars())
    if run_scoped:
        return run_scoped
    return list(session.execute(
        select(TicketWorkspace)
        .where(TicketWorkspace.ticket_id == run.ticket_id)
        .order_by(TicketWorkspace.position)
    ).scalars())


def _diff_to_json(d: RunDiff) -> dict:
    """Serialize a RunDiff to a JSON-friendly dict."""
    return {
        "files": [
            {
                "path": f.path,
                "old_path": f.old_path,
                "new_path": f.new_path,
                "binary": f.binary,
                "lines_added": f.lines_added,
                "lines_deleted": f.lines_deleted,
                "hunks": [
                    {
                        "kind": h.kind,
                        "gutter": h.gutter,
                        "text": h.text,
                        "line_no_old": h.line_no_old,
                        "line_no_new": h.line_no_new,
                    }
                    for h in f.hunks
                ],
            }
            for f in d.files
        ],
        "total_added": d.total_added,
        "total_deleted": d.total_deleted,
        "total_files": d.total_files,
        "truncated": d.truncated,
        "hidden_files": d.hidden_files,
        "hidden_lines": d.hidden_lines,
        "error": d.error,
        "branch": d.branch,
        "base_sha": d.base_sha,
        "head_sha": d.head_sha,
        "repo_root": d.repo_root,
    }
