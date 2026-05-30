"""HTML page for recurring cron jobs at ``/cron``.

Follows the page-route pattern in ``ticket_page.py`` / ``archive.py``: a
cookie-or-bearer-auth GET that renders the list, plus form-style POST routes
that redirect back to ``/cron`` (HTML forms can't send PATCH/DELETE).

Worktree options are intentionally NOT offered here — cron is directory-only in
v1 (see ``domain/cron_jobs``). The board ticket modal carries no cron controls.

The ``force_run`` flag makes a job materialize ``run_now=True`` tickets, which
the scheduler dispatches past the queue and outside the active-hours window.
"""
from __future__ import annotations

import zoneinfo
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.db.models import Ticket
from nightdesk.domain.cron_jobs import (
    CronJobNotFound, InvalidCronJob,
    create_cron_job, delete_cron_job, disable_cron_job, enable_cron_job,
    fire_now, get_cron_job, list_cron_jobs, list_fires, update_cron_job,
    validate_schedule, validate_timezone,
)
from nightdesk.domain.profiles import list_profiles


def _parse_extra_dirs_json(raw: str) -> list[dict]:
    """Parse the resources picker's JSON ([{path, mode}]) into additional_dirs.

    ``mode`` is 'rw' or 'ro' (defaults to 'rw'). Non-absolute paths are dropped;
    duplicates are collapsed. The strict path remains the JSON API."""
    import json
    try:
        rows = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        p = str(row.get("path", "")).strip()
        if not p.startswith("/") or p in seen:
            continue
        seen.add(p)
        mode = row.get("mode", "rw")
        out.append({"path": p, "mode": "ro" if mode == "ro" else "rw"})
    return out


# Cached at import; the IANA set is stable for the process lifetime.
_TIMEZONES = sorted(zoneinfo.available_timezones())


def build_router(get_session, bearer_token: str, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(tags=["cron-page"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    def _fires_payload(session: Session, job_id: str) -> list[dict]:
        out: list[dict] = []
        for f in list_fires(session, job_id, limit=25):
            status = None
            if f.ticket_id:
                t = session.get(Ticket, f.ticket_id)
                status = t.status if t else None
            out.append({
                "fire_at": f.fire_at,
                "ticket_id": f.ticket_id,
                "ticket_status": status,
                "skipped_reason": f.skipped_reason,
            })
        return out

    def _render_page(request, session, *, pane_mode, selected=None,
                     error=None, partial=False):
        """Render the two-pane cron page (or just the right pane for HX swaps).

        ``pane_mode`` is one of ``'empty'``, ``'view'``, ``'edit'``, ``'new'``.
        Mirrors the profiles page: the sidebar always lists every job so users
        flip between jobs with one click; ``partial`` returns only the pane.
        """
        jobs = list_cron_jobs(session)
        profiles = list_profiles(session)
        ctx: dict = {
            "title": "Cron",
            "active_page": "cron",
            "jobs": jobs,
            "profiles": profiles,
            "profile_names": {p.id: p.name for p in profiles},
            "selected_id": selected.id if selected is not None else None,
            "pane_mode": pane_mode,
            "error": error,
            "now": datetime.now(timezone.utc),
            "timezones": _TIMEZONES,
        }
        if pane_mode == "view" and selected is not None:
            ctx["job"] = selected
            ctx["fires"] = _fires_payload(session, selected.id)
        elif pane_mode == "edit" and selected is not None:
            ctx["job"] = selected
            ctx["form_action"] = f"/cron/{selected.id}/update"
        elif pane_mode == "new":
            ctx["job"] = None
            ctx["form_action"] = "/cron"
        template = "partials/cron_pane.html" if partial else "cron.html"
        return templates.TemplateResponse(request, template, ctx)

    @router.get("/cron", response_class=HTMLResponse, dependencies=[auth])
    async def cron_page(
        request: Request,
        error: Optional[str] = Query(default=None),
        session: Session = Depends(get_session),
    ):
        jobs = list_cron_jobs(session)
        if jobs:
            return _render_page(request, session, pane_mode="view",
                                selected=jobs[0], error=error)
        return _render_page(request, session, pane_mode="empty", error=error)

    @router.get("/cron/new", response_class=HTMLResponse, dependencies=[auth])
    async def cron_new(request: Request, session: Session = Depends(get_session)):
        partial = request.headers.get("HX-Request") == "true"
        return _render_page(request, session, pane_mode="new", partial=partial)

    @router.get("/cron/{cid}", response_class=HTMLResponse, dependencies=[auth])
    async def cron_view(cid: str, request: Request,
                        session: Session = Depends(get_session)):
        try:
            job = get_cron_job(session, cid)
        except CronJobNotFound:
            raise HTTPException(404, "not found")
        partial = request.headers.get("HX-Request") == "true"
        return _render_page(request, session, pane_mode="view",
                            selected=job, partial=partial)

    @router.get("/cron/{cid}/edit", response_class=HTMLResponse, dependencies=[auth])
    async def cron_edit(cid: str, request: Request,
                        session: Session = Depends(get_session)):
        try:
            job = get_cron_job(session, cid)
        except CronJobNotFound:
            raise HTTPException(404, "not found")
        partial = request.headers.get("HX-Request") == "true"
        return _render_page(request, session, pane_mode="edit",
                            selected=job, partial=partial)

    @router.post("/cron", dependencies=[auth])
    async def create_form(
        title: str = Form(...),
        prompt: str = Form(""),
        profile_id: str = Form(...),
        source_path: str = Form(...),
        schedule: str = Form(...),
        timezone: str = Form("UTC"),
        priority: int = Form(0),
        workspace_mode: str = Form("directory"),
        overlap_policy: str = Form("skip_if_active"),
        force_run: bool = Form(False),
        extra_dirs_json: str = Form("[]"),
        session: Session = Depends(get_session),
    ):
        try:
            job = create_cron_job(
                session,
                title=title.strip(),
                prompt=prompt,
                profile_id=profile_id,
                source_path=source_path.strip(),
                schedule=schedule,
                timezone=timezone,
                priority=priority,
                workspace_mode=workspace_mode,
                overlap_policy=overlap_policy,
                force_run=force_run,
                additional_dirs=_parse_extra_dirs_json(extra_dirs_json),
            )
        except InvalidCronJob as e:
            return RedirectResponse(url=f"/cron?error={e}", status_code=303)
        return RedirectResponse(url=f"/cron/{job.id}", status_code=303)

    @router.post("/cron/{cid}/update", dependencies=[auth])
    async def update_form(
        cid: str,
        title: str = Form(...),
        prompt: str = Form(""),
        profile_id: str = Form(...),
        source_path: str = Form(...),
        schedule: str = Form(...),
        timezone: str = Form("UTC"),
        priority: int = Form(0),
        workspace_mode: str = Form("directory"),
        overlap_policy: str = Form("skip_if_active"),
        force_run: bool = Form(False),
        extra_dirs_json: str = Form("[]"),
        session: Session = Depends(get_session),
    ):
        try:
            update_cron_job(
                session, cid,
                title=title.strip(),
                prompt=prompt,
                profile_id=profile_id,
                source_path=source_path.strip(),
                schedule=schedule,
                timezone=timezone,
                priority=priority,
                workspace_mode=workspace_mode,
                overlap_policy=overlap_policy,
                force_run=force_run,
                additional_dirs=_parse_extra_dirs_json(extra_dirs_json),
            )
        except CronJobNotFound:
            raise HTTPException(404, "not found")
        except InvalidCronJob as e:
            return RedirectResponse(url=f"/cron/{cid}?error={e}", status_code=303)
        return RedirectResponse(url=f"/cron/{cid}", status_code=303)

    @router.post("/cron/{cid}/enable", dependencies=[auth])
    async def enable_form(cid: str, session: Session = Depends(get_session)):
        try:
            enable_cron_job(session, cid)
        except CronJobNotFound:
            raise HTTPException(404, "not found")
        return RedirectResponse(url=f"/cron/{cid}", status_code=303)

    @router.post("/cron/{cid}/disable", dependencies=[auth])
    async def disable_form(cid: str, session: Session = Depends(get_session)):
        try:
            disable_cron_job(session, cid)
        except CronJobNotFound:
            raise HTTPException(404, "not found")
        return RedirectResponse(url=f"/cron/{cid}", status_code=303)

    @router.post("/cron/{cid}/fire-now", dependencies=[auth])
    async def fire_now_form(cid: str, session: Session = Depends(get_session)):
        try:
            fire_now(session, cid)
        except CronJobNotFound:
            raise HTTPException(404, "not found")
        except InvalidCronJob as e:
            return RedirectResponse(url=f"/cron/{cid}?error={e}", status_code=303)
        return RedirectResponse(url=f"/cron/{cid}", status_code=303)

    @router.post("/cron/{cid}/delete", dependencies=[auth])
    async def delete_form(cid: str, session: Session = Depends(get_session)):
        try:
            delete_cron_job(session, cid)
        except CronJobNotFound:
            raise HTTPException(404, "not found")
        return RedirectResponse(url="/cron", status_code=303)

    @router.post("/cron/preview", response_class=HTMLResponse, dependencies=[auth])
    async def cron_preview(
        request: Request,
        schedule: str = Form(""),
        timezone: str = Form("UTC"),
    ):
        try:
            sched = validate_schedule(schedule)
            tzname = validate_timezone(timezone or "UTC")
        except InvalidCronJob as exc:
            return templates.TemplateResponse(
                request, "partials/cron_preview.html", {"error": str(exc)})
        tz = ZoneInfo(tzname)
        base = datetime.now(tz)
        it = croniter(sched, base)
        fires = [it.get_next(datetime).strftime("%a %b %d, %I:%M %p") for _ in range(5)]
        return templates.TemplateResponse(
            request, "partials/cron_preview.html",
            {"error": None, "fires": fires, "timezone": tzname})

    return router
