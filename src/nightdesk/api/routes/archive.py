from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.domain.projects import ProjectNotFound, get_project_by_slug, list_projects
from nightdesk.domain.profiles import list_profiles
from nightdesk.domain.runs import list_runs
from nightdesk.domain.search import FTS5SearchBackend
from nightdesk.domain.tickets import (
    InvalidTransition,
    TicketNotFound,
    list_tickets,
    unarchive,
)


_OUTCOMES = ("any", "success", "failed", "cancelled", "worker_crash")


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _relative_time(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def _latest_finished_run(session: Session, ticket_id: str):
    """Return the most-recent finished run for a ticket, or None."""
    runs = list_runs(session, ticket_id=ticket_id)
    for r in runs:
        if r.finished_at is not None:
            return r
    return None


def _build_rows(
    session: Session,
    *,
    outcome: str,
    profile_id: str,
    since: Optional[date],
    until: Optional[date],
    q: str,
    project_id: Optional[str],
) -> list[dict]:
    """Assemble archive rows after applying every filter."""
    matching_ids: Optional[set[str]] = None
    if q.strip():
        hits = FTS5SearchBackend(session).search(q, limit=200)
        matching_ids = {h.id for h in hits}
        if not matching_ids:
            return []

    profile_filter = profile_id or None
    tickets = list_tickets(
        session,
        status="archived",
        profile_id=profile_filter,
        project_id=project_id,
        limit=1000,
    )

    since_dt = (
        datetime.combine(since, time.min, tzinfo=timezone.utc) if since else None
    )
    # Make ``until`` inclusive of the chosen day by snapping to end-of-day.
    until_dt = (
        datetime.combine(until, time.max, tzinfo=timezone.utc) if until else None
    )

    rows: list[dict] = []
    for t in tickets:
        if matching_ids is not None and t.id not in matching_ids:
            continue
        run = _latest_finished_run(session, t.id)
        run_outcome = run.exit_status if run else None
        finished_at = run.finished_at if run else None
        # SQLite returns naive datetimes; assume UTC so comparisons work.
        if finished_at is not None and finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=timezone.utc)

        if outcome != "any" and run_outcome != outcome:
            continue
        if since_dt is not None:
            if finished_at is None or finished_at < since_dt:
                continue
        if until_dt is not None:
            if finished_at is None or finished_at > until_dt:
                continue

        rows.append({
            "id": t.id,
            "title": t.title,
            "profile_id": t.profile_id,
            "project_id": t.project_id,
            "priority": t.priority,
            "outcome": run_outcome,
            "finished_at": finished_at,
            "finished_rel": _relative_time(finished_at),
        })
    return rows


def _resolve_project_filter(session: Session, project: str) -> tuple[Optional[str], object | None]:
    if project == "none":
        return "null", None
    if not project:
        return None, None
    try:
        selected = get_project_by_slug(session, project)
    except ProjectNotFound:
        return "__missing__", None
    return selected.id, selected


def _resolve_filters(
    outcome: str,
    profile_id: str,
    since: Optional[str],
    until: Optional[str],
    q: str,
    session: Session | None = None,
    project: str = "",
) -> dict:
    if outcome not in _OUTCOMES:
        outcome = "any"
    # Use the UTC date, not the local one: run timestamps (finished_at) are
    # stored in UTC, and the until/since bounds are compared against them after
    # snapping to UTC start/end-of-day. Defaulting ``until`` to the local date
    # in a timezone behind UTC produced an end-of-day bound earlier than a
    # just-finished UTC run, silently hiding recently-archived tickets.
    today = datetime.now(timezone.utc).date()
    since_d = _parse_date(since)
    if since is None:
        since_d = today - timedelta(days=30)
    until_d = _parse_date(until)
    if until is None:
        until_d = today
    project_id, selected_project = _resolve_project_filter(session, project or "") if session is not None else (None, None)
    return {
        "outcome": outcome,
        "profile_id": profile_id or "",
        "since": since_d,
        "until": until_d,
        "q": q or "",
        "project": project or "",
        "project_id": project_id,
        "selected_project": selected_project,
    }


def _profile_lookup(session: Session) -> dict[str, str]:
    return {p.id: p.name for p in list_profiles(session)}


def build_router(get_session, bearer_token: str, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(tags=["archive"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("/archive", response_class=HTMLResponse, dependencies=[auth])
    async def archive_page(
        request: Request,
        outcome: str = Query(default="any"),
        profile_id: str = Query(default=""),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        q: str = Query(default=""),
        project: str = Query(default=""),
        session: Session = Depends(get_session),
    ):
        filters = _resolve_filters(outcome, profile_id, since, until, q, session, project)
        rows = _build_rows(
            session,
            outcome=filters["outcome"],
            profile_id=filters["profile_id"],
            since=filters["since"],
            until=filters["until"],
            q=filters["q"],
            project_id=filters["project_id"],
        )
        return templates.TemplateResponse(request, "archive.html", {
            "title": "Archive",
            "filters": filters,
            "filters_iso": {
                "since": filters["since"].isoformat() if filters["since"] else "",
                "until": filters["until"].isoformat() if filters["until"] else "",
            },
            "rows": rows,
            "profiles": list_profiles(session),
            "projects": list_projects(session),
            "selected_project": filters["selected_project"],
            "profile_names": _profile_lookup(session),
            "project_names": {p.id: p.name for p in list_projects(session, include_archived=True)},
            "outcomes": _OUTCOMES,
        })

    @router.get("/archive/rows", response_class=HTMLResponse, dependencies=[auth])
    async def archive_rows(
        request: Request,
        outcome: str = Query(default="any"),
        profile_id: str = Query(default=""),
        since: Optional[str] = Query(default=None),
        until: Optional[str] = Query(default=None),
        q: str = Query(default=""),
        project: str = Query(default=""),
        session: Session = Depends(get_session),
    ):
        filters = _resolve_filters(outcome, profile_id, since, until, q, session, project)
        rows = _build_rows(
            session,
            outcome=filters["outcome"],
            profile_id=filters["profile_id"],
            since=filters["since"],
            until=filters["until"],
            q=filters["q"],
            project_id=filters["project_id"],
        )
        return templates.TemplateResponse(request, "partials/archive_table.html", {
            "rows": rows,
            "profile_names": _profile_lookup(session),
            "project_names": {p.id: p.name for p in list_projects(session, include_archived=True)},
        })

    @router.post("/archive/tickets/{tid}/unarchive", dependencies=[auth])
    async def archive_unarchive(tid: str, session: Session = Depends(get_session)):
        try:
            unarchive(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return RedirectResponse(url="/archive", status_code=303)

    return router
