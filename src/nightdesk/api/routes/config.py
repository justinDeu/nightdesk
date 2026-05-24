from __future__ import annotations

from datetime import datetime, time, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_bearer
from nightdesk.api.schemas import ConfigOut, ConfigUpdate, WorkerStatusOut
from nightdesk.db.models import ConfigRow, Run, Ticket, WorkerHeartbeat
from nightdesk.worker.scheduler import in_window


_STALE_THRESHOLD_SECONDS = 30.0


def _ensure_config(session: Session, *, worktree_root: str, transcript_root: str) -> ConfigRow:
    row = session.get(ConfigRow, 1)
    if row is None:
        row = ConfigRow(id=1, worktree_root=worktree_root, transcript_root=transcript_root)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def build_router(get_session, bearer_token: str, *, worktree_root: str,
                  transcript_root: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["config"],
        dependencies=[Depends(require_bearer(bearer_token))],
    )

    @router.get("/config", response_model=ConfigOut)
    async def show(session: Session = Depends(get_session)):
        row = _ensure_config(session, worktree_root=worktree_root, transcript_root=transcript_root)
        return row

    @router.patch("/config", response_model=ConfigOut)
    async def update(payload: ConfigUpdate, session: Session = Depends(get_session)):
        row = _ensure_config(session, worktree_root=worktree_root, transcript_root=transcript_root)
        for k, v in payload.model_dump().items():
            if v is not None:
                setattr(row, k, v)
        # Empty-string webhook URL means "clear it" — set to None.
        if row.notify_webhook_url is not None and not row.notify_webhook_url.strip():
            row.notify_webhook_url = None
        session.commit()
        session.refresh(row)
        return row

    @router.get("/worker/status", response_model=WorkerStatusOut)
    async def worker_status(session: Session = Depends(get_session)):
        cfg = _ensure_config(
            session, worktree_root=worktree_root, transcript_root=transcript_root
        )
        hb = session.get(WorkerHeartbeat, 1)

        # Count actual worker activity from unfinished Run rows (not
        # Ticket.status='running'), so a ticket wedged in 'running' without
        # a Run row doesn't lie about the worker doing work.
        total_running = session.scalar(
            select(func.count()).select_from(Run).where(Run.finished_at.is_(None))
        ) or 0
        run_now_running = session.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.finished_at.is_(None), Run.started_as_run_now.is_(True))
        ) or 0
        normal_running = max(0, total_running - run_now_running)

        try:
            ws = _parse_hhmm(cfg.window_start)
            we = _parse_hhmm(cfg.window_end)
            now = datetime.now(timezone.utc)
            in_win = in_window(ws, we, now)
        except Exception:
            in_win = False

        stale = True
        last_seen_at = None
        host = None
        pid = None
        if hb is not None:
            last_seen_at = hb.last_seen_at
            host = hb.host
            pid = hb.pid
            if last_seen_at is not None:
                # SQLite round-trips datetimes without tzinfo; normalize.
                aware = last_seen_at if last_seen_at.tzinfo else last_seen_at.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - aware).total_seconds()
                stale = age > _STALE_THRESHOLD_SECONDS

        return WorkerStatusOut(
            host=host,
            pid=pid,
            last_seen_at=last_seen_at,
            stale=stale,
            in_window=in_win,
            window_start=cfg.window_start,
            window_end=cfg.window_end,
            max_parallel=cfg.max_parallel,
            normal_running=normal_running,
            run_now_running=run_now_running,
            total_running=total_running,
            running_count=total_running,
        )

    return router
