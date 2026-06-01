"""Header bar UI endpoints.

Exposes the polled worker status pill and the live search dropdown. These
small HTML partials are consumed by the header in ``templates/base.html``
via HTMX. They are intentionally separate from the JSON API endpoints so
the templates can evolve without API contract churn.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.db.models import ConfigRow, Run, Ticket, WorkerHeartbeat
from nightdesk.domain.analytics import compute_spend_status
from nightdesk.domain.query import parse_query, search_tickets, suggest_values
from nightdesk.domain.search import hit_from_ticket
from nightdesk.worker.scheduler import in_window


_STALE_THRESHOLD_SECONDS = 30.0
_MIN_QUERY_LEN = 2


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _collect_worker_status(session: Session, *, worktree_root: str,
                            transcript_root: str) -> dict[str, Any]:
    """Inlined duplicate of ``config_routes.worker_status`` logic.

    Kept inline (not refactored into ``domain/``) because the function is
    small and lives at the API + DB boundary. Refactoring would force the
    existing config endpoint to change too, expanding blast radius.
    """
    cfg = session.get(ConfigRow, 1)
    if cfg is None:
        cfg = ConfigRow(id=1, worktree_root=worktree_root,
                         transcript_root=transcript_root)
        session.add(cfg)
        session.commit()
        session.refresh(cfg)

    hb = session.get(WorkerHeartbeat, 1)

    # See nightdesk.api.routes.config.worker_status for the rationale: count
    # actual worker activity from unfinished Run rows so wedged tickets don't
    # inflate the pill. Fetch the running runs once and derive the counts from
    # that same list so the pill total can never disagree with the hover panel.
    running_runs = [
        {
            "id": r.id,
            "ticket_id": r.ticket_id,
            "ticket_title": r.ticket.title if r.ticket else "(unknown)",
            "profile_name": (r.ticket.profile.name
                             if r.ticket and r.ticket.profile
                             else "(unknown)"),
            "pid": r.pid,
            "started_at": r.started_at,
            "started_as_run_now": r.started_as_run_now,
        }
        for r in (
            session.execute(
                select(Run)
                .where(Run.finished_at.is_(None))
                .options(joinedload(Run.ticket).joinedload(Ticket.profile))
                .order_by(Run.started_at)
            )
            .scalars()
            .unique()
            .all()
        )
    ]
    total_running = len(running_runs)
    run_now_running = sum(1 for r in running_runs if r["started_as_run_now"])
    normal_running = max(0, total_running - run_now_running)

    now = datetime.now(timezone.utc)
    try:
        ws = _parse_hhmm(cfg.window_start)
        we = _parse_hhmm(cfg.window_end)
        in_win = in_window(ws, we, now)
    except Exception:
        in_win = False

    spend = compute_spend_status(session, now=now)

    stale = True
    host = None
    pid = None
    last_seen_at = None
    if hb is not None:
        host = hb.host
        pid = hb.pid
        last_seen_at = hb.last_seen_at
        if last_seen_at is not None:
            aware = last_seen_at if last_seen_at.tzinfo else last_seen_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - aware).total_seconds()
            stale = age > _STALE_THRESHOLD_SECONDS

    return {
        "host": host,
        "pid": pid,
        "last_seen_at": last_seen_at,
        "stale": stale,
        "in_window": in_win,
        "window_start": cfg.window_start,
        "window_end": cfg.window_end,
        "max_parallel": cfg.max_parallel,
        "normal_running": normal_running,
        "run_now_running": run_now_running,
        "total_running": total_running,
        "running_runs": running_runs,
        "day_spend_usd": spend.day_spend_usd,
        "month_spend_usd": spend.month_spend_usd,
    }


def build_router(get_session, bearer_token: str, templates: Jinja2Templates,
                  *, worktree_root: str, transcript_root: str) -> APIRouter:
    router = APIRouter(tags=["header"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("/header/search", response_class=HTMLResponse, dependencies=[auth])
    async def header_search(
        request: Request,
        q: str = Query(default=""),
        session: Session = Depends(get_session),
        project: str = Query(default=""),
    ):
        query = (q or "").strip()
        # Back-compat: a legacy ?project= becomes a project= term in the language.
        if project and "project=" not in query:
            query = (f"project={project} " + query).strip()
        if len(query) < _MIN_QUERY_LEN:
            hits: list = []
        else:
            tickets = search_tickets(session, parse_query(query), limit=20)
            hits = [hit_from_ticket(t) for t in tickets]
        return templates.TemplateResponse(request, "partials/search_results.html", {
            "hits": hits, "q": query,
        })

    @router.get("/search/suggest", dependencies=[auth])
    async def search_suggest(
        field: str = Query(...),
        resource: str = Query(default="ticket"),
        q: str = Query(default=""),
        session: Session = Depends(get_session),
    ):
        """Existing values for a field, for facet dropdowns and autocomplete."""
        values = suggest_values(session, resource=resource, field=field, prefix=q)
        return JSONResponse({"field": field, "values": values})

    @router.get("/header/worker-pill", response_class=HTMLResponse,
                 dependencies=[auth])
    async def header_worker_pill(request: Request,
                                   session: Session = Depends(get_session)):
        status = _collect_worker_status(session, worktree_root=worktree_root,
                                          transcript_root=transcript_root)
        return templates.TemplateResponse(request, "partials/worker_pill.html", {
            "status": status,
        })

    @router.get("/header/spend-chip", response_class=HTMLResponse,
                 dependencies=[auth])
    async def header_spend_chip(request: Request,
                                 session: Session = Depends(get_session)):
        """Top-line cost summary for the header (today + month spend)."""
        status = _collect_worker_status(session, worktree_root=worktree_root,
                                          transcript_root=transcript_root)
        return templates.TemplateResponse(request, "partials/spend_chip.html", {
            "status": status,
        })

    return router
