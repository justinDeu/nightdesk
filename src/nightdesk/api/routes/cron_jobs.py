"""JSON API for recurring cron jobs.

Mirrors the ``build_router(get_session, bearer_token)`` pattern used by
``tickets.py``. Routes live under ``/api/v1/cron-jobs`` with bearer auth.

A cron job is a ticket template plus a schedule; ``fire-now`` materializes a
queued ticket immediately and records a fire row. Generated tickets are
``run_now=False`` unless the job has ``force_run=True`` (then ``run_now=True``,
dispatched past the queue and active-hours window). ``DELETE`` removes only the
schedule (and its audit rows), never the generated tickets. Invalid cron expr /
unknown tz / worktree workspace -> 422.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.api.schemas import CronJobCreate, CronJobOut, CronJobUpdate, TicketOut
from nightdesk.domain.cron_jobs import (
    CronJobNotFound, InvalidCronJob,
    create_cron_job, delete_cron_job, disable_cron_job, enable_cron_job,
    fire_now, get_cron_job, list_cron_jobs, update_cron_job,
)


def _coerce_dirs(payload_dirs):
    if payload_dirs is None:
        return None
    return [
        d.model_dump() if hasattr(d, "model_dump") else dict(d) for d in payload_dirs
    ]


def build_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/cron-jobs",
        tags=["cron-jobs"],
        dependencies=[Depends(require_token_cookie_or_bearer(bearer_token))],
    )

    @router.post("", response_model=CronJobOut, status_code=status.HTTP_201_CREATED)
    async def create(payload: CronJobCreate, session: Session = Depends(get_session)):
        data = payload.model_dump()
        data["additional_dirs"] = _coerce_dirs(payload.additional_dirs) or []
        try:
            return create_cron_job(session, **data)
        except InvalidCronJob as e:
            raise HTTPException(422, str(e))

    @router.get("", response_model=list[CronJobOut])
    async def lst(
        enabled: bool | None = Query(default=None),
        session: Session = Depends(get_session),
    ):
        return list_cron_jobs(session, enabled=enabled)

    @router.get("/{cid}", response_model=CronJobOut)
    async def show(cid: str, session: Session = Depends(get_session)):
        try:
            return get_cron_job(session, cid)
        except CronJobNotFound:
            raise HTTPException(404, "not found")

    @router.patch("/{cid}", response_model=CronJobOut)
    async def update(cid: str, payload: CronJobUpdate, session: Session = Depends(get_session)):
        fields = {k: v for k, v in payload.model_dump().items() if v is not None}
        if "additional_dirs" in fields:
            fields["additional_dirs"] = _coerce_dirs(payload.additional_dirs) or []
        try:
            return update_cron_job(session, cid, **fields)
        except CronJobNotFound:
            raise HTTPException(404, "not found")
        except InvalidCronJob as e:
            raise HTTPException(422, str(e))

    @router.post("/{cid}/enable", response_model=CronJobOut)
    async def enable(cid: str, session: Session = Depends(get_session)):
        try:
            return enable_cron_job(session, cid)
        except CronJobNotFound:
            raise HTTPException(404, "not found")

    @router.post("/{cid}/disable", response_model=CronJobOut)
    async def disable(cid: str, session: Session = Depends(get_session)):
        try:
            return disable_cron_job(session, cid)
        except CronJobNotFound:
            raise HTTPException(404, "not found")

    @router.post("/{cid}/fire-now", response_model=TicketOut,
                 status_code=status.HTTP_201_CREATED)
    async def fire_now_route(cid: str, session: Session = Depends(get_session)):
        try:
            return fire_now(session, cid)
        except CronJobNotFound:
            raise HTTPException(404, "not found")
        except InvalidCronJob as e:
            raise HTTPException(422, str(e))

    @router.post("/{cid}/fire-now-and-run", response_model=TicketOut,
                 status_code=status.HTTP_201_CREATED)
    async def fire_now_and_run_route(cid: str, session: Session = Depends(get_session)):
        """Materialize a ticket from the template and run it immediately.

        Same as fire-now, but the generated ticket is forced ``run_now=True`` so
        the scheduler dispatches it right away (queue bypass) — for testing a
        cron's output without waiting for its schedule window.
        """
        try:
            return fire_now(session, cid, run=True)
        except CronJobNotFound:
            raise HTTPException(404, "not found")
        except InvalidCronJob as e:
            raise HTTPException(422, str(e))

    @router.delete("/{cid}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete(cid: str, session: Session = Depends(get_session)):
        try:
            delete_cron_job(session, cid)
        except CronJobNotFound:
            raise HTTPException(404, "not found")

    return router
