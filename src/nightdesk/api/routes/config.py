from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_bearer
from nightdesk.api.schemas import (
    ConfigOut,
    ConfigUpdate,
    ScheduleWindowCreate,
    ScheduleWindowOut,
    ScheduleWindowUpdate,
    ScheduleWindowsReplace,
    WorkerStatusOut,
)
from nightdesk.db.models import ConfigRow, Run, ScheduleWindow, Ticket, WorkerHeartbeat
from nightdesk.domain.analytics import compute_spend_status
from nightdesk.worker.scheduler import capacity_for, window_matches


_STALE_THRESHOLD_SECONDS = 30.0


def _ensure_config(session: Session, *, worktree_root: str, transcript_root: str) -> ConfigRow:
    row = session.get(ConfigRow, 1)
    if row is None:
        row = ConfigRow(id=1, worktree_root=worktree_root, transcript_root=transcript_root)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def _list_windows(session: Session) -> list[ScheduleWindow]:
    return list(
        session.scalars(
            select(ScheduleWindow).order_by(ScheduleWindow.position.asc(), ScheduleWindow.id.asc())
        )
    )


def _config_out(session: Session, row: ConfigRow) -> ConfigOut:
    windows = [ScheduleWindowOut.model_validate(w) for w in _list_windows(session)]
    return ConfigOut(
        window_start=row.window_start,
        window_end=row.window_end,
        max_parallel=row.max_parallel,
        worktree_root=row.worktree_root,
        transcript_root=row.transcript_root,
        worktree_base_ref=row.worktree_base_ref,
        notify_webhook_url=row.notify_webhook_url,
        schedule_timezone=row.schedule_timezone,
        windows=windows,
        toolchain_presets=row.toolchain_presets or {},
    )


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
        return _config_out(session, row)

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
        return _config_out(session, row)

    # --- Schedule windows CRUD ---------------------------------------------
    # Multi-window schedule model. When several windows match the current
    # time/day the scheduler resolves max_parallel from the highest-precedence
    # one — the first in `position` order (dragged to the top in Settings).

    @router.get("/config/windows", response_model=list[ScheduleWindowOut])
    async def list_windows(session: Session = Depends(get_session)):
        return _list_windows(session)

    @router.post("/config/windows", response_model=ScheduleWindowOut, status_code=201)
    async def create_window(payload: ScheduleWindowCreate,
                            session: Session = Depends(get_session)):
        window = ScheduleWindow(**payload.model_dump())
        session.add(window)
        session.commit()
        session.refresh(window)
        return window

    @router.patch("/config/windows/{window_id}", response_model=ScheduleWindowOut)
    async def update_window(window_id: int, payload: ScheduleWindowUpdate,
                            session: Session = Depends(get_session)):
        window = session.get(ScheduleWindow, window_id)
        if window is None:
            raise HTTPException(404, "schedule window not found")
        for k, v in payload.model_dump(exclude_unset=True).items():
            setattr(window, k, v)
        session.commit()
        session.refresh(window)
        return window

    @router.delete("/config/windows/{window_id}", status_code=204)
    async def delete_window(window_id: int, session: Session = Depends(get_session)):
        window = session.get(ScheduleWindow, window_id)
        if window is None:
            raise HTTPException(404, "schedule window not found")
        session.delete(window)
        session.commit()
        return None

    @router.put("/config/windows", response_model=list[ScheduleWindowOut])
    async def replace_windows(payload: ScheduleWindowsReplace,
                              session: Session = Depends(get_session)):
        cfg = _ensure_config(session, worktree_root=worktree_root,
                             transcript_root=transcript_root)
        cfg.schedule_timezone = payload.timezone
        for existing in _list_windows(session):
            session.delete(existing)
        session.flush()
        for i, w in enumerate(payload.windows):
            data = w.model_dump()
            data["position"] = i
            session.add(ScheduleWindow(**data))
        session.commit()
        return _list_windows(session)

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

        tz_name = cfg.schedule_timezone or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(timezone.utc)
        windows = _list_windows(session)
        cap = capacity_for(windows, now, tz)
        in_win = cap is not None
        active = next((w.label for w in windows if window_matches(w, now, tz)), None)
        effective_max = cap if cap is not None else 0

        spend = compute_spend_status(session, now=now, tz=tz)

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
            window_start=None,
            window_end=None,
            max_parallel=effective_max,
            active_window=active,
            schedule_timezone=tz_name,
            normal_running=normal_running,
            run_now_running=run_now_running,
            total_running=total_running,
            running_count=total_running,
            day_spend_usd=spend.day_spend_usd,
            month_spend_usd=spend.month_spend_usd,
        )

    return router
