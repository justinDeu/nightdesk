from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_bearer
from nightdesk.api.schemas import RunOut
from nightdesk.db.models import TicketWorkspace
from nightdesk.domain.diff import RunDiff, compute_run_diff, diff_repo_path
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

        Looks up the TicketWorkspace associated with the run and uses its
        git metadata (repo_root, base_sha, head_sha) to compute the diff.
        """
        try:
            run = get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "not found")

        # Find the workspace for this run.
        ws = session.execute(
            select(TicketWorkspace)
            .where(TicketWorkspace.run_id == rid)
            .order_by(TicketWorkspace.position)
            .limit(1)
        ).scalar_one_or_none()

        if ws is None:
            # Fall back to ticket-level workspace.
            ws = session.execute(
                select(TicketWorkspace)
                .where(TicketWorkspace.ticket_id == run.ticket_id)
                .order_by(TicketWorkspace.position)
                .limit(1)
            ).scalar_one_or_none()

        repo_path = diff_repo_path(ws) if ws is not None else ""
        if ws is None or not repo_path:
            return JSONResponse({
                "files": [],
                "total_added": 0,
                "total_deleted": 0,
                "total_files": 0,
                "truncated": False,
                "hidden_files": 0,
                "hidden_lines": 0,
                "error": "no git workspace found for this run",
                "branch": "",
                "base_sha": "",
                "head_sha": "",
                "repo_root": "",
            })

        result = compute_run_diff(
            repo_root=repo_path,
            base_sha=ws.base_sha,
            head_sha=ws.head_sha,
            branch=ws.branch,
        )
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
