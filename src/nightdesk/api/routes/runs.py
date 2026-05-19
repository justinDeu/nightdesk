from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_bearer
from nightdesk.api.schemas import RunOut
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
